"""The write review's counters in `bettermemory eval`.

Unit D1 (TESTS FIRST item 7; MEASUREMENT, the review section of the eval
output; PREDICTIONS P11 to P15). Every row is made by the real
`memory_write` handler on a scratch store with tau set explicitly, and the
log's clock is moved by the test so a chain can wait thirty minutes. A
chain is a root review with its follow-ups; it is unanswered when no write
carrying one of its review_ids arrives within 30 minutes and no closing
call arrives under DESIGN item 3's rule.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import identity, review
from bettermemory.config import Config, ScopesConfig, StorageConfig
from bettermemory.eval import compute_eval, parse_window_bound
from bettermemory.models import generate_ulid
from bettermemory.store import Store
from bettermemory.time_utils import isoformat_utc

from ._review_rows import (
    BACKUP_NEW,
    BACKUP_OLD,
    BACKUP_RESTATED,
    FACTS,
    PORT_NEW,
    PORT_OLD,
    STATUS_NEW,
    STATUS_OLD,
    Clock,
    Harness,
    fix_origin_and_identity,
    install_clock,
    set_tau,
)
from .test_daemon import _auth, mcp_session, running_app


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    fix_origin_and_identity(monkeypatch)
    set_tau(monkeypatch)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return install_clock(monkeypatch, offset=timedelta(hours=1))


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path / "store", monkeypatch)


def _review(h: Harness, clock: Clock, **kwargs: Any) -> dict[str, Any]:
    """The review section, judged an hour after the clock's time unless
    the test says otherwise."""
    kwargs.setdefault("now", clock.now + timedelta(hours=1))
    report = compute_eval(
        memories=h.store.load_all(), events=h.store.iter_events(), **kwargs
    )
    assert report.review is not None
    return report.review


async def test_review_counters_issued_answered_unanswered_after_30_minutes_by_client(
    h: Harness, clock: Clock
) -> None:
    h.as_host("host-a")
    first = await h.write(FACTS[0])
    clock.advance(minutes=5)
    assert (await h.answer(first, FACTS[0]))["status"] == "committed"
    await h.write(FACTS[1])  # never answered
    clock.advance(minutes=31)
    late = await h.write(FACTS[2])
    clock.advance(minutes=40)
    # The daemon still decides a late answer; eval counts the chain
    # unanswered, since nothing arrived within 30 minutes.
    assert (await h.answer(late, FACTS[2]))["status"] == "committed"

    h.as_host("host-b")
    clock.advance(minutes=1)
    first_b = await h.write(FACTS[3])
    clock.advance(minutes=29)
    assert (await h.answer(first_b, FACTS[3]))["status"] == "committed"
    clock.advance(minutes=1)
    await h.write(FACTS[4])  # host-b, not answered

    section = _review(h, clock)
    assert section["chains"] == {
        "issued": 5,
        "answered": 2,
        "unanswered": 3,
        "pending": 0,
        "closed_by_call": 0,
    }
    assert section["answer_window_minutes"] == 30
    a, b = section["by_client"]["host-a"], section["by_client"]["host-b"]
    assert (a["issued"], a["answered"], a["unanswered"]) == (3, 1, 2)
    assert a["unanswered_share"] == round(2 / 3, 4)
    assert (b["issued"], b["answered"], b["unanswered"]) == (2, 1, 1)
    assert section["unanswered_share"] == 0.6

    # Judged before the last chain's 30 minutes have passed, it is pending,
    # not unanswered.
    early = _review(h, clock, now=clock.now + timedelta(minutes=10))
    assert (
        early["by_client"]["host-b"]["pending"],
        early["by_client"]["host-b"]["unanswered"],
    ) == (1, 0)
    assert early["chains"]["unanswered"] == 2


async def test_review_counters_undeclared_client_counted_as_none(
    h: Harness, clock: Clock
) -> None:
    h.as_host(None)
    await h.reviewed_commit(FACTS[0])
    await h.write(FACTS[1])
    section = _review(h, clock)
    none = section["by_client"][review.UNDECLARED]
    assert (none["issued"], none["answered"], none["unanswered"]) == (2, 1, 1)
    assert list(section["by_client"]) == [review.UNDECLARED]
    assert list(section["by_model"]) == [review.UNDECLARED]


async def test_review_counters_by_model_header(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    config = Config(
        storage=StorageConfig(directory=str(tmp_path / "store")),
        scopes=ScopesConfig(),
    )

    def call(content: str, answers: dict[str, Any] | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"content": content, "scopes": ["tools"]}
        if answers is not None:
            args["review"] = answers
        return args

    def payload(result: Any) -> dict[str, Any]:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        return json.loads("".join(getattr(b, "text", "") for b in result.content))

    async with running_app(config, store) as client:
        headers = _auth(
            **{identity.HEADER_CLIENT: "http-host", identity.HEADER_MODEL: "writer-a"}
        )
        async with mcp_session(client, headers=headers) as session:
            first = payload(await session.call_tool("memory_write", call(FACTS[0])))
            answers = {
                "review_id": first["review"]["review_id"],
                "relation": {e["id"]: "unrelated" for e in first["review"]["related"]},
                "unsafe": 0,
                "secret": 0,
            }
            done = payload(
                await session.call_tool("memory_write", call(FACTS[0], answers))
            )
            assert done["status"] == "committed"
        client.headers.pop(identity.HEADER_MODEL, None)
        headers_b = _auth(
            **{identity.HEADER_CLIENT: "http-host", identity.HEADER_MODEL: "writer-b"}
        )
        async with mcp_session(client, headers=headers_b) as session:
            second = payload(await session.call_tool("memory_write", call(FACTS[1])))
            assert second["status"] == "review"

    issued = [e for e in store.iter_events() if e.get("kind") == review.REVIEW_ISSUED]
    assert [e["model"] for e in issued] == ["writer-a", "writer-b"]
    from datetime import datetime, timezone

    report = compute_eval(
        memories=store.load_all(),
        events=store.iter_events(),
        now=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert report.review is not None
    by_model = report.review["by_model"]
    assert set(by_model) == {"writer-a", "writer-b"}
    assert (by_model["writer-a"]["issued"], by_model["writer-a"]["answered"]) == (1, 1)
    assert (by_model["writer-b"]["issued"], by_model["writer-b"]["unanswered"]) == (
        1,
        1,
    )
    assert report.review["by_client"]["http-host"]["issued"] == 2


async def test_follow_up_review_counts_in_its_root_chain(
    h: Harness, clock: Clock
) -> None:
    old = h.seed(BACKUP_OLD)
    first = await h.write(BACKUP_NEW)
    assert [e["id"] for e in first["review"]["related"]] == [old]
    clock.advance(minutes=20)
    # Answers that name no related id get a follow-up review for the
    # missing id, chained to the root.
    follow = await h.answer(first, BACKUP_NEW, only=[])
    assert follow["status"] == "review"
    clock.advance(minutes=20)
    res = await h.answer(follow, BACKUP_NEW)
    assert res["status"] == "committed"

    rows = h.events(review.REVIEW_ISSUED)
    assert [r["parent_review_id"] for r in rows] == [None, first["review"]["review_id"]]
    section = _review(h, clock)
    # One chain, answered within 30 minutes of its latest review (40
    # minutes after the root), in three calls.
    assert section["chains"]["issued"] == 1
    assert section["chains"]["answered"] == 1
    assert section["calls_per_committed_write"] == 3.0
    assert section["response_chars_n"] == 2


async def test_pre_answered_share(h: Harness, clock: Clock) -> None:
    res = await h.pre_answer(FACTS[0])
    assert res["status"] == "committed"
    clock.advance(minutes=1)
    await h.reviewed_commit(FACTS[1])
    section = _review(h, clock)
    assert section["chains"]["issued"] == 2
    assert section["chains"]["answered"] == 2
    assert section["pre_answered"] == 1
    assert section["pre_answered_share"] == 0.5
    # The pre-answered chain took one call; the reviewed one two.
    assert section["calls_per_committed_write"] == 1.5


async def test_pre_answered_zero_scores_counter(h: Harness, clock: Clock) -> None:
    old = h.seed(BACKUP_OLD)
    h.as_host("host-a", "writer-1")
    assert (await h.pre_answer(FACTS[0]))["status"] == "committed"  # zero
    h.as_host("host-a", "writer-2")
    assert (await h.pre_answer(FACTS[1], unsafe=0.2))["status"] == "committed"
    assert old in h.related_ids(BACKUP_NEW)
    res = await h.pre_answer(BACKUP_NEW, labels={old: "conflict"})
    assert res["status"] == "committed"
    h.as_host("host-b", "writer-1")
    assert (await h.pre_answer(FACTS[2]))["status"] == "committed"  # zero
    # A reviewed (not pre-answered) zero answer is not counted.
    await h.reviewed_commit(FACTS[3])

    section = _review(h, clock)
    zero = section["pre_answered_zero"]
    assert zero["total"] == 2
    assert zero["by_client"] == {"host-a": 1, "host-b": 1}
    assert zero["by_model"] == {"writer-1": 2, "writer-2": 0}
    assert section["pre_answered"] == 4
    assert section["by_client"]["host-a"]["pre_answered_zero"] == 1


async def test_answer_without_review_id_closes_open_chain_by_related_ids(
    h: Harness, clock: Clock
) -> None:
    old = h.seed(BACKUP_OLD)
    h.as_host("host-a")
    first = await h.write(BACKUP_NEW)
    assert [e["id"] for e in first["review"]["related"]] == [old]
    clock.advance(minutes=5)
    # The model answers without the review_id: a pre-answered call that
    # names every related id of the open chain's row.
    assert (await h.pre_answer(BACKUP_NEW))["status"] == "committed"

    # Another client's open chain is not closed by host-a's call.
    other = "Grafana dashboards for the queue live in the observability repo."
    h.as_host("host-b")
    clock.advance(minutes=1)
    await h.write(other)
    h.as_host("host-a")
    clock.advance(minutes=1)
    assert (await h.pre_answer(other))["status"] == "committed"

    section = _review(h, clock)
    a = section["by_client"]["host-a"]
    b = section["by_client"]["host-b"]
    # host-a: the closed chain plus the pre-answered write that closed
    # nothing (its own chain).
    assert (a["issued"], a["answered"], a["closed_by_call"]) == (2, 2, 1)
    assert a["pre_answered"] == 1
    assert (b["issued"], b["unanswered"]) == (1, 1)
    assert section["chains"]["closed_by_call"] == 1
    # The closed chain took two calls: the review and the closing call.
    assert a["calls_per_committed_write"] == 1.5


async def test_fabricated_or_reused_review_id_counted_as_pre_answered(
    h: Harness, clock: Clock
) -> None:
    h.as_host("host-a", "writer-1")
    # A self-attesting write sent with a review_id no review returned.
    assert h.related_ids(FACTS[0]) == []
    res = await h.write(
        FACTS[0],
        review={
            "review_id": generate_ulid(),
            "relation": {},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert res["status"] == "committed"
    clock.advance(minutes=1)
    first = await h.write(FACTS[1])
    clock.advance(minutes=1)
    assert (await h.answer(first, FACTS[1]))["status"] == "committed"
    clock.advance(minutes=1)
    # A different write re-sending the answered chain's review_id.
    res = await h.write(
        FACTS[2],
        review={
            "review_id": first["review"]["review_id"],
            "relation": {i: "unrelated" for i in h.related_ids(FACTS[2])},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert res["status"] == "committed"

    assert len(h.events("write")) == 3
    section = _review(h, clock)
    assert section["chains"]["issued"] == 3
    assert section["chains"]["answered"] == 3
    assert section["chains"]["unanswered"] == 0
    assert section["committed_chains"] == {"chains": 3, "calls": 4}
    assert section["pre_answered"] == 2
    assert section["pre_answered_zero"]["total"] == 2
    assert section["pre_answered_zero"]["by_model"] == {"writer-1": 2}
    assert section["calls_per_committed_write"] == 1.333


async def test_calls_per_committed_write_excludes_refused_chains(
    h: Harness, clock: Clock
) -> None:
    await h.reviewed_commit(FACTS[0])  # 2 calls
    await h.pre_answer(FACTS[1])  # 1 call
    first = await h.write(FACTS[2])
    refused = await h.answer(first, FACTS[2], unsafe=0.9)  # 2 calls, refused
    assert refused["status"] == "instruction_warning"
    await h.write(FACTS[3])  # 1 call, unanswered

    section = _review(h, clock)
    assert section["calls_per_committed_write"] == 1.5
    assert section["committed_chains"] == {"chains": 2, "calls": 3}
    assert section["refused_chains"] == {"chains": 1, "calls": 2}
    assert section["unanswered_chains"] == {"chains": 1, "calls": 1}


async def _one_of_each_class(h: Harness) -> dict[str, str]:
    """A secret refusal, an unsafe refusal, a restatement, a review link,
    an update_suggested and a review filing, each through the handler."""
    old = h.seed(BACKUP_OLD)
    first = await h.write(FACTS[0])
    assert (await h.answer(first, FACTS[0], secret=0.9))["status"] == (
        "credential_warning"
    )
    first = await h.write(FACTS[1])
    assert (await h.answer(first, FACTS[1], unsafe=0.5))["status"] == (
        "instruction_warning"
    )
    first = await h.write(BACKUP_RESTATED)
    restated = await h.answer(first, BACKUP_RESTATED, labels={old: "restates"})
    assert restated["status"] == "duplicate"
    first = await h.write(BACKUP_NEW)
    linked = await h.answer(first, BACKUP_NEW, labels={old: "update"})
    assert linked["supersedes"] == [{"id": old, "evidence": "review"}]

    from .test_write_review import _long_billing_record

    long_id = h.seed(_long_billing_record(0))
    content = "The billing service moved eu-west invoices onto the ledger cluster."
    first = await h.write(content)
    suggested = await h.answer(first, content, labels={long_id: "update"})
    assert suggested["update_suggested"] == [long_id]

    target = h.seed(STATUS_OLD)
    first = await h.write(STATUS_NEW)
    assert target in [e["id"] for e in first["review"]["related"]]
    filed = await h.answer(first, STATUS_NEW, labels={target: "conflict"})
    # The cue code leaves this pair alone, so the filing is the review's own,
    # the one the counters read from the row's review_filings.
    assert [(f["id"], f.get("detector")) for f in filed["conflicts_filed"]] == [
        (target, review.DETECTOR_REVIEW)
    ]
    (row,) = [r for r in h.events("write") if r.get("id") == filed["id"]]
    assert [f["detector"] for f in row["review_filings"]] == [review.DETECTOR_REVIEW]
    return {"old": old, "long": long_id, "target": target}


async def test_refusals_links_filings_by_class(h: Harness, clock: Clock) -> None:
    await _one_of_each_class(h)
    section = _review(h, clock)
    assert section["actions"] == {
        review.ACTION_COMMIT: 3,
        review.ACTION_SECRET: 1,
        review.ACTION_UNSAFE: 1,
        review.ACTION_RESTATES: 1,
    }
    assert section["links"] == 1
    assert section["update_suggested"] == 1
    assert section["filings"] == {
        review.DETECTOR_REVIEW: 1,
        review.DETECTOR_REVIEW_DISAGREES: 0,
    }
    assert section["refused_chains"]["chains"] == 3


async def test_filings_count_review_and_review_disagrees_per_100_committed_writes(
    h: Harness, clock: Clock
) -> None:
    old = h.seed(BACKUP_OLD)
    port = h.seed(PORT_OLD)
    first = await h.write(BACKUP_NEW)
    assert (await h.answer(first, BACKUP_NEW, labels={old: "conflict"}))[
        "conflicts_filed"
    ][0]["detector"] == review.DETECTOR_REVIEW
    # The cue code links PORT_NEW over PORT_OLD; an unrelated answer on
    # that target files the pair as a disagreement.
    first = await h.write(PORT_NEW)
    res = await h.answer(first, PORT_NEW, labels={port: "unrelated"})
    assert [f.get("detector") for f in res["conflicts_filed"]] == [
        review.DETECTOR_REVIEW_DISAGREES
    ]
    await h.reviewed_commit(FACTS[0])
    await h.pre_answer(FACTS[1])

    section = _review(h, clock)
    assert section["committed_chains"]["chains"] == 4
    assert section["filings_per_100_committed"] == {
        review.DETECTOR_REVIEW: 25.0,
        review.DETECTOR_REVIEW_DISAGREES: 25.0,
        "total": 50.0,
    }
    year, week, _ = clock.now.isocalendar()
    assert section["filings_by_week"] == {
        f"{year}-W{week:02d}": {
            review.DETECTOR_REVIEW: 1,
            review.DETECTOR_REVIEW_DISAGREES: 1,
        }
    }


async def test_response_chars_p50_from_review_issued_rows(
    h: Harness, clock: Clock
) -> None:
    h.seed(BACKUP_OLD)
    h.seed(PORT_OLD)
    for content in (FACTS[0], BACKUP_NEW, PORT_NEW):
        await h.write(content)
    await h.pre_answer(FACTS[1])  # a pre-answered row has no response size
    rows = h.events(review.REVIEW_ISSUED)
    sizes = [r["response_chars"] for r in rows if not r["pre_answered"]]
    assert len(sizes) == 3
    assert all("response_chars" not in r for r in rows if r["pre_answered"])
    section = _review(h, clock)
    assert section["response_chars_n"] == 3
    assert section["response_chars_p50"] == float(statistics.median(sizes))
    assert len(set(sizes)) > 1


async def test_recorded_helped_rate_unchanged_by_reviews(
    h: Harness, clock: Clock
) -> None:
    from ._mcp import call_tool

    memory_id = h.seed(FACTS[10])
    await call_tool(
        h.server, "memory_search", {"query": "translation pipeline content strings"}
    )
    await call_tool(
        h.server,
        "memory_record_use",
        {
            "memory_ids": [memory_id],
            "outcome": "applied",
            "claim_excerpts": [FACTS[10]],
        },
    )
    now = clock.now + timedelta(hours=1)
    before = compute_eval(
        memories=h.store.load_all(), events=h.store.iter_events(), now=now
    )
    assert before.memory_helped_rate.denominator > 0
    await _one_of_each_class(h)
    await h.pre_answer(FACTS[5])
    await h.write(FACTS[6])
    after = compute_eval(
        memories=h.store.load_all(), events=h.store.iter_events(), now=now
    )
    assert after.review is not None and after.review["chains"]["issued"] == 8
    for name in ("memory_helped_rate", "endorsement_rate", "silent_miss_rate"):
        assert getattr(after, name).to_dict() == getattr(before, name).to_dict()


def _run_eval_cli(
    store_dir: Path, args: list[str], capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    from bettermemory.server import main as server_main

    saved_argv, saved_dir = sys.argv[:], os.environ.get("BETTERMEMORY_DIR")
    os.environ["BETTERMEMORY_DIR"] = str(store_dir)
    sys.argv = ["bettermemory", "eval", "--json", *args]
    try:
        server_main()
    finally:
        sys.argv = saved_argv
        if saved_dir is None:
            os.environ.pop("BETTERMEMORY_DIR", None)
        else:
            os.environ["BETTERMEMORY_DIR"] = saved_dir
    return json.loads(capsys.readouterr().out)


async def test_eval_absolute_since_until_bound_every_review_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The whole window sits in the past, so the CLI (which judges as of the
    # real now) has passed every chain's 30 minutes.
    clock = install_clock(monkeypatch, offset=-timedelta(days=10))
    h = Harness(tmp_path / "store", monkeypatch)
    old = h.seed(BACKUP_OLD)

    await h.reviewed_commit(FACTS[0])  # before the window
    await h.pre_answer(FACTS[1])
    await h.write(FACTS[2])
    clock.advance(hours=2)
    since = clock.now
    clock.advance(minutes=10)
    first = await h.write(BACKUP_NEW)  # inside
    filed = await h.answer(first, BACKUP_NEW, labels={old: "conflict"})
    assert filed["status"] == "committed"
    await h.pre_answer(FACTS[3])  # inside, pre-answered with zero scores
    await h.write(FACTS[4])  # inside, unanswered
    clock.advance(hours=2)
    until = clock.now
    clock.advance(hours=2)
    await h.reviewed_commit(FACTS[5])  # after
    await h.pre_answer(FACTS[6])
    await h.write(FACTS[7])

    doc = _run_eval_cli(
        h.root,
        ["--since", isoformat_utc(since), "--until", isoformat_utc(until)],
        capsys,
    )
    assert doc["window"] == {
        "since": isoformat_utc(since),
        "until": isoformat_utc(until),
    }
    section = doc["review"]
    assert section["chains"] == {
        "issued": 3,
        "answered": 2,
        "unanswered": 1,
        "pending": 0,
        "closed_by_call": 0,
    }
    assert section["pre_answered"] == 1
    assert section["pre_answered_zero"]["total"] == 1
    assert section["response_chars_n"] == 2
    assert section["calls_per_committed_write"] == 1.5
    assert section["filings"][review.DETECTOR_REVIEW] == 1
    assert section["filings_per_100_committed"]["total"] == 50.0
    assert sum(sum(w.values()) for w in section["filings_by_week"].values()) == 1

    whole = _run_eval_cli(h.root, ["--since", "all"], capsys)
    assert whole["review"]["chains"]["issued"] == 9
    assert whole["window"] == {"since": None, "until": None}
    # The relative forms still parse.
    relative = _run_eval_cli(h.root, ["--since", "30d"], capsys)
    assert relative["review"]["chains"]["issued"] == 9
    assert relative["window_seconds"] == 30 * 86400
    assert parse_window_bound("12h") == timedelta(hours=12)
    assert parse_window_bound("all") is None
    for bad in ("2026-10-01T00:00:00", "yesterday", "0d"):
        with pytest.raises(ValueError):
            parse_window_bound(bad)


async def test_chain_issued_near_until_judged_from_rows_up_to_30_minutes_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = install_clock(monkeypatch, offset=-timedelta(days=10))
    h = Harness(tmp_path / "store", monkeypatch)
    since = clock.now
    clock.advance(hours=1)
    until = clock.now + timedelta(minutes=10)
    answered = await h.write(FACTS[0])  # 10 minutes before --until
    clock.advance(minutes=5)
    unanswered = await h.write(FACTS[1])  # 5 minutes before --until
    clock.advance(minutes=15)
    # 10 minutes after --until: inside the 30 minutes the chain is judged by.
    assert (await h.answer(answered, FACTS[0]))["status"] == "committed"
    clock.advance(minutes=31)
    # 41 minutes after --until: a row past the judging horizon is not read.
    assert (await h.answer(unanswered, FACTS[1]))["status"] == "committed"
    await h.write(FACTS[2])  # after --until: not in the window

    from datetime import datetime, timezone

    report = compute_eval(
        memories=h.store.load_all(),
        events=h.store.iter_events(),
        now=datetime.now(timezone.utc),
        since=since,
        until=until,
    )
    section = report.review
    assert section is not None
    assert section["chains"]["issued"] == 2
    assert section["chains"]["answered"] == 1
    assert section["chains"]["unanswered"] == 1
    assert section["judged_through"] == isoformat_utc(until + timedelta(minutes=30))
    assert report.window_end == until
