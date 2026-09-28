"""Tests for unit S3's serving levers and harness.

S3's levers live in the bench service (`bench/aml/service.py`): the tiered
and dry fills, whole-session serving from the session log, and the rescue
lane passed through to the engine. `bench/longmemeval/s3.py` measures
them. The cases pinned here are the ones that would change what is served
or counted without failing loudly:

- the tiered fill's first tier drifting from the S0 arm's served rounds,
  or a later tier's round joining an earlier tier's session group
- the dry fill engaging when the pool was not dry
- the session log dropping or reshaping a message, or its date format
  drifting from the dataset's
- whole sessions ranked, budgeted, ordered or rendered other than as
  declared, or their prompt drifting from upstream's flat-session one
- a session counted as covered whose answer turn is not in the history
- the M dev stream losing or splitting an element at a buffer edge
- the choice rule or a prediction's bounds read wrong

Everything is hermetic: stores live under `tmp_path`, and nothing here
calls a model. The comparison with the real upstream file runs only where
the receipts hold it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BENCH = _ROOT / "bench"


def _load(name: str, path: Path) -> ModuleType:
    if str(_BENCH) not in sys.path:
        sys.path.insert(0, str(_BENCH))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


s3 = _load("bench_longmemeval_s3", _BENCH / "longmemeval" / "s3.py")
svc_mod = sys.modules["aml.service"]

DAY = 86_400_000
T0 = 1_684_584_000_000  # 2023-05-20 12:00 UTC, a Saturday


def _inst() -> dict[str, Any]:
    """Four sessions on four days; the question's words hit sessions 1 and
    3, and its evidence sits in 1 and 2 (2 shares no word with it)."""
    return {
        "question_id": "q1",
        "question_type": "multi-session",
        "question": "Which city did I move to?",
        "answer": "Lisbon",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_dates": [
            "2023/05/20 (Sat) 12:00",
            "2023/05/21 (Sun) 12:00",
            "2023/05/22 (Mon) 12:00",
            "2023/05/23 (Tue) 12:00",
        ],
        "haystack_session_ids": ["s_tea", "answer_move", "answer_flat", "s_city"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like green tea."},
                {"role": "assistant", "content": "Green tea is nice."},
            ],
            [
                {
                    "role": "user",
                    "content": "I made the move to Lisbon last week.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": 'Welcome to "Lisbon"!'},
            ],
            [
                {
                    "role": "user",
                    "content": "My new flat has a balcony over the river.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Lovely views, I bet."},
            ],
            [
                {"role": "user", "content": "Which city has the best trams?"},
                {"role": "assistant", "content": "Lisbon's are famous."},
            ],
        ],
        "answer_session_ids": ["answer_move", "answer_flat"],
    }


def _ingest(root: Path, inst: dict[str, Any]) -> Any:
    svc = s3.service_for(root)
    user, adds = s3.aml_run.adds_for(inst)
    for add in adds:
        svc.add(user_id=user, **add)
    return svc


# ---------------------------------------------------------------- the log


def test_the_session_log_keeps_each_add_whole_less_its_timestamp(
    tmp_path: Path,
) -> None:
    inst = _inst()
    svc = _ingest(tmp_path, inst)
    log = svc._sessions(svc._user(s3.s0.user_id(inst)))
    assert [e["session_id"] for e in log] == inst["haystack_session_ids"]
    assert log[1]["messages"] == [
        {"role": "user", "content": "I made the move to Lisbon last week."},
        {"role": "assistant", "content": 'Welcome to "Lisbon"!'},
    ]
    assert [svc_mod._fmt_ts(e["ts"]) for e in log] == inst["haystack_dates"]
    assert all(len(e["round_ids"]) == 1 for e in log)
    assert s3.aligned_log(svc, inst) == log


def test_a_store_without_keep_sessions_writes_no_log_and_serves_no_sessions(
    tmp_path: Path,
) -> None:
    inst = _inst()
    svc = s3.l1.service_for(tmp_path)
    user, adds = s3.aml_run.adds_for(inst)
    for add in adds:
        svc.add(user_id=user, **add)
    assert not (svc._user(user).root / svc_mod.SESSION_LOG).exists()
    svc.unit = "sessions"
    with pytest.raises(ValueError, match="session log"):
        svc.search(user, inst["question"], 10)


def test_aligned_log_refuses_a_log_that_is_not_the_haystack(tmp_path: Path) -> None:
    inst = _inst()
    svc = _ingest(tmp_path, inst)
    other = _inst()
    other["haystack_sessions"][2][1]["content"] = "Nice."
    with pytest.raises(SystemExit, match="not session 2"):
        s3.aligned_log(svc, other)
    other = _inst()
    other["haystack_dates"][3] = "2023/05/23 (Tue) 12:01"
    with pytest.raises(SystemExit, match="dated"):
        s3.aligned_log(svc, other)


# ---------------------------------------------------------------- sessions


def test_a_session_block_is_upstreams_json_rendering() -> None:
    entry = {
        "ts": T0,
        "messages": [{"role": "user", "content": 'Caf\u00e9 "Lua"\nnext'}],
    }
    assert svc_mod.session_block(3, entry) == (
        "\n### Session 3:\nSession Date: 2023/05/20 (Sat) 12:00\nSession Content:\n"
        '\n[{"role": "user", "content": "Caf\\u00e9 \\"Lua\\"\\nnext"}]\n'
    )


def test_adjacent_sessions_are_nearest_to_a_hit_then_later_first() -> None:
    log = [{"ts": T0 + d * DAY} for d in (0, 1, 2, 3, 4, 6)]
    # hits on days 2 and 6: day 1 and day 3 are one day from day 2, day 4
    # is two days from both, day 0 two from day 2
    assert svc_mod.adjacent_sessions(log, [2, 5]) == [3, 1, 4, 0]
    # no hit at all: the latest first
    assert svc_mod.adjacent_sessions(log, []) == [5, 4, 3, 2, 1, 0]


def test_whole_sessions_serve_hits_then_fill_in_date_order(tmp_path: Path) -> None:
    inst = _inst()
    svc = _ingest(tmp_path, inst)
    user = s3.s0.user_id(inst)
    svc.unit, svc.serve, svc.budget = "sessions", 100, 0
    hits = svc.search(user, inst["question"], 100)
    served = s3.session_numbers(hits)
    assert served == sorted(served)  # date order
    assert all(not h["filled"] for h in hits)
    assert set(served) == {1, 3}  # "city" and "move" hit sessions 3 and 1 only
    svc.session_fill = "adjacent"
    hits = svc.search(user, inst["question"], 100)
    assert s3.session_numbers(hits) == [0, 1, 2, 3]
    assert [h["filled"] for h in hits] == [True, False, True, False]
    assert s3.history(hits).count("### Session") == 4
    assert "### Session 4:" in s3.history(hits)


def test_a_session_too_long_for_the_budget_is_skipped_for_one_that_fits(
    tmp_path: Path,
) -> None:
    inst = _inst()
    inst["haystack_sessions"][3][1]["content"] = "x" * 3000
    svc = _ingest(tmp_path, inst)
    us = svc._user(s3.s0.user_id(inst))
    log = svc._sessions(us)
    size = [len(svc_mod.session_block(999, e)) for e in log]
    assert size[2] > size[0] + 1
    svc.budget, svc.session_fill = size[1] + size[0] + 1, "adjacent"
    ranked = [(log[1]["round_ids"][0], 0.9), (log[3]["round_ids"][0], 0.5)]
    hits = svc._serve_sessions(us, ranked)
    # session 1 first; 3 too long beside it; of the fill, 2 (one day from
    # both hits, the later of the two nearest) does not fit and 0 does
    assert s3.session_numbers(hits) == [0, 1]
    assert [h["filled"] for h in hits] == [True, False]
    assert len(s3.history(hits)) <= svc.budget


def test_session_coverage_needs_every_answer_session_and_its_turn(
    tmp_path: Path,
) -> None:
    inst = _inst()
    svc = _ingest(tmp_path, inst)
    user = s3.s0.user_id(inst)
    svc.unit, svc.serve, svc.budget = "sessions", 100, 0
    hits = svc.search(user, inst["question"], 100)
    got = s3.session_coverage(inst, hits)
    assert got["covered"] is False and got["missing"] == [(2, 0, 0)]
    svc.session_fill = "adjacent"
    hits = svc.search(user, inst["question"], 100)
    assert s3.session_coverage(inst, hits)["covered"] is True
    hits[1]["content"] = hits[1]["content"].replace("Lisbon last", "Porto last")
    assert s3.session_coverage(inst, hits)["missing"] == [(1, 0, 0)]


def test_unique_ids_rename_only_the_later_copies() -> None:
    assert s3.unique_ids(["a", "b", "a", "a"]) == ["a", "b", "a#1", "a#2"]


def test_the_session_prompt_equals_upstreams_flat_session_prompt(
    tmp_path: Path,
) -> None:
    if not s3.l1.UPSTREAM_GENERATION.exists():
        pytest.skip("the pinned upstream file is not in the receipts here")
    prepare = s3.f1.upstream_prepare(
        s3.l1.UPSTREAM_GENERATION, s3.l1.UPSTREAM_GENERATION_SHA256
    )
    inst = _inst()
    # one session twice under one id, on its own date, as 13 S haystacks have
    inst["haystack_session_ids"][0] = "s_city"
    svc = _ingest(tmp_path, inst)
    svc.unit, svc.serve, svc.budget, svc.session_fill = "sessions", 100, 0, "adjacent"
    hits = svc.search(s3.s0.user_id(inst), inst["question"], 100)
    ours = s3.l1.reading_prompt(
        s3.history(hits), inst["question_date"], inst["question"]
    )
    assert ours == s3.upstream_flat_session(prepare, inst, hits)
    assert "has_answer" in json.dumps(inst)  # upstream worked on a copy


# ---------------------------------------------------------------- the fills


def _round_service(root: Path, fill: str, top_k: int, budget: int = 0) -> Any:
    svc = s3.service_for(root)
    s3.s0.configure(svc, s3.s0.Arm("session", top_k, fill, "none"), budget)
    return svc


def _big_inst() -> dict[str, Any]:
    """Six sessions of three rounds: the question hits one round each in
    sessions 0 and 4."""
    inst = _inst()
    inst["haystack_dates"] = [f"2023/05/{20 + k} (---) 12:00" for k in range(6)]
    inst["haystack_session_ids"] = [f"s{k}" for k in range(6)]
    sessions = []
    for k in range(6):
        turns = []
        for j in range(3):
            said = "I moved city." if (k, j) in {(0, 1), (4, 2)} else f"Note {k}.{j}."
            turns += [
                {"role": "user", "content": said},
                {"role": "assistant", "content": f"Reply {k}.{j}."},
            ]
        sessions.append(turns)
    inst["haystack_sessions"] = sessions
    inst["answer_session_ids"] = []
    return inst


def _served_ids(svc: Any, inst: dict[str, Any], k: int) -> list[str]:
    return [h["id"] for h in svc.search(s3.s0.user_id(inst), inst["question"], k)]


def test_the_tiered_fill_serves_the_neighbors_arm_first_then_the_rest(
    tmp_path: Path,
) -> None:
    inst = _big_inst()
    base = _ingest(tmp_path, inst)
    user = s3.s0.user_id(inst)
    positions = s3.s0.round_positions(base, inst)
    neighbors = _served_ids(_round_service(tmp_path, "neighbors", 4), inst, 4)
    tiered = _served_ids(_round_service(tmp_path, "tiered", 4), inst, 4)
    assert user and tiered[:4] == neighbors
    assert sorted(tiered) == sorted(positions)  # every round, no budget
    rest = [positions[m][0] for m in tiered[4:]]
    # the hit sessions' two rounds tier 1 left, then whole unhit sessions
    # nearest in time to a hit: days 1, 3 and 5 are one day from one (the
    # later first), day 2 two days
    assert set(rest[:2]) <= {0, 4}
    assert list(dict.fromkeys(rest[2:])) == [5, 3, 1, 2]


def test_a_later_tier_never_joins_an_earlier_tiers_session_group(
    tmp_path: Path,
) -> None:
    inst = _big_inst()
    base = _ingest(tmp_path, inst)
    positions = s3.s0.round_positions(base, inst)
    tiered = _served_ids(_round_service(tmp_path, "tiered", 2), inst, 2)
    # tier 1 holds the two hits only (the pool is two rounds); their
    # sessions' other rounds come after them, not merged into their groups
    assert {positions[m] for m in tiered[:2]} == {(0, 1), (4, 2)}
    assert set(positions[m][0] for m in tiered[2:6]) == {0, 4}


def test_the_dry_fill_engages_only_under_a_dry_pool(tmp_path: Path) -> None:
    inst = _big_inst()
    _ingest(tmp_path, inst)
    full = _served_ids(_round_service(tmp_path, "neighbors", 4), inst, 4)
    assert _served_ids(_round_service(tmp_path, "dry", 4), inst, 4) == full
    # a pool larger than the hits and their neighbors: dry, so every round
    dry = _served_ids(_round_service(tmp_path, "dry", 50), inst, 50)
    assert len(dry) == 18
    assert dry[:6] == _served_ids(_round_service(tmp_path, "neighbors", 50), inst, 50)


def test_the_budget_caps_the_tiered_fill(tmp_path: Path) -> None:
    inst = _big_inst()
    _ingest(tmp_path, inst)
    svc = _round_service(tmp_path, "tiered", 4, budget=400)
    hits = svc.search(s3.s0.user_id(inst), inst["question"], 4)
    assert 0 < sum(len(h["content"]) for h in hits) <= 400


def test_the_rescue_lane_reaches_the_engine(tmp_path: Path, monkeypatch: Any) -> None:
    inst = _inst()
    svc = _ingest(tmp_path, inst)
    seen: list[bool] = []
    real = svc_mod.run_search

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["rescue_expansion"])
        return real(*args, **kwargs)

    monkeypatch.setattr(svc_mod, "run_search", spy)
    svc.search(s3.s0.user_id(inst), inst["question"], 10)
    svc.rescue_expansion = True
    svc.search(s3.s0.user_id(inst), inst["question"], 10)
    assert seen == [False, True]


# ---------------------------------------------------------------- the M dev


def test_the_array_stream_crosses_buffer_edges(tmp_path: Path) -> None:
    items = [
        {"q": i, "text": "x" * (i * 7), "u": "caf\u00e9 \u2028"} for i in range(40)
    ]
    path = tmp_path / "a.json"
    path.write_text(" [\n" + ",\n ".join(json.dumps(i) for i in items) + "\n] ")
    assert list(s3.iter_json_array(path, chunk=16)) == items
    path.write_text("[]")
    assert list(s3.iter_json_array(path, chunk=16)) == []


def test_the_same_question_ignores_ms_own_question_date() -> None:
    s = {"question_id": "q", "question_type": "t", "question": "Q?", "answer": 4}
    m = {**s, "answer": "4", "question_date": "later"}
    assert s3.same_question({**s, "question_date": "early"}, m)
    assert not s3.same_question(s, {**m, "question": "Other?"})


# ---------------------------------------------------------------- the choice


def _stats(covered: int, chars: float) -> dict[str, Any]:
    return {"covered": covered, "mean_chars": chars}


def test_the_rule_takes_coverage_on_both_sets_before_size() -> None:
    s = {
        "a": _stats(143, 168_000.0),
        "b": _stats(143, 159_000.0),
        "c": _stats(142, 1.0),
    }
    m = {"a": _stats(127, 0.0), "b": _stats(126, 0.0), "c": _stats(126, 0.0)}
    assert s3.choose(s, m, ["a", "b", "c"], 126) == "a"
    m["a"] = _stats(125, 0.0)  # below the S0 arm's M coverage: not eligible
    assert s3.choose(s, m, ["a", "b", "c"], 126) == "b"
    assert s3.choose(s, m, ["a"], 126) is None


def test_the_s3a_predictions_follow_their_declared_bounds() -> None:
    def graded(**n: Any) -> list[str]:
        base = {
            "s_covered": 143,
            "m_covered": 127,
            "s0_m_covered": 126,
            "s_mean": 150_000.0,
        }
        return [p["verdict"] for p in s3.predictions({**base, **n})]

    assert graded() == ["HIT", "HIT", "HIT"]
    assert graded(s_covered=141)[0] == "MISSED"
    assert graded(m_covered=126)[1] == "MISSED"
    assert graded(s_mean=176_000.0)[2] == "MISSED"
    assert graded(s_mean=170_000.0)[2] not in ("HIT", "MISSED")


# ---------------------------------------------------------------- the reads


def test_the_briefing_is_l1s_wording_with_three_spans_changed() -> None:
    text = s3.reader_instructions(Path("/w/b.txt"), Path("/w/a.json"), 181_000, 2_300)
    assert "it lists one prompt file path." in text
    assert "up to about 181,000 characters and 2,300 lines" in text
    assert "open with the answer itself in one plain sentence" in text
    assert "that first sentence says so" in text
    assert "/w/b.txt" in text and "/w/a.json" in text
    base = s3.l1.READER_INSTRUCTIONS
    for span in (s3._L1_BATCH, s3._L1_SIZE, s3._L1_FORM):
        base = base.replace(span, "")
    rest = s3.READER_TEMPLATE
    for span in (s3._S3_BATCH, s3._S3_SIZE, s3._S3_FORM):
        rest = rest.replace(span, "")
    assert rest == base  # nothing else changed
    assert "reasoning" not in s3._S3_FORM  # v1's safeguard trigger stays out


def test_the_s0_arms_prompt_is_s1s(tmp_path: Path) -> None:
    inst = _inst()
    svc = _ingest(tmp_path, inst)
    row, prompt = s3.served_prompt(svc, inst, s3.S0_ARM, "dev")
    s1_row, s1_prompt = s3.s1.build(s3.s1.service_for(tmp_path), inst)
    assert prompt == s1_prompt
    assert row["context_chars"] == s1_row["context_chars"]
    assert row["covered"] == s1_row["covered"]
    assert row["split"] == "dev" and row["gold"] == "Lisbon"


def test_each_question_gets_its_own_reader(tmp_path: Path) -> None:
    built = [
        ({"question_id": q, "prompt_chars": 1_500 * (i + 1)}, "x\n" * (i + 1) + q)
        for i, q in enumerate(("q1", "q2", "q3"))
    ]
    meta = s3.write_reads(tmp_path, built, "S3b", {"arm": "a"})
    assert meta["batches"] == [["q1"], ["q2"], ["q3"]]
    assert meta["stated_chars"] == 5_000 and meta["arm"] == "a"
    listed = (tmp_path / "batches" / "batch_01.txt").read_text(encoding="utf-8")
    assert listed == f"{tmp_path / 'prompts_wrapped' / 'q2'}.txt\n"
    assert (tmp_path / "prompts" / "q3.txt").read_text(encoding="utf-8").endswith("q3")
    assert meta["instructions"][2].count("batch_02") == 2


def _rows(qids: list[str]) -> list[dict[str, Any]]:
    return [{"question_id": q, "abstention": q.endswith("_abs")} for q in qids]


def test_gate2_passes_on_all_right_or_only_the_out_of_reach_miss() -> None:
    qids = ["a", "b_abs", s3.DEV_OUT_OF_REACH]
    s1v = {name: {q: q != s3.DEV_OUT_OF_REACH for q in qids} for name in s3.JUDGES}
    every = {name: dict.fromkeys(qids, True) for name in s3.JUDGES}
    assert s3.gate2(_rows(qids), every, s1v)["passed"] is True
    one_off = {name: {**every[name], s3.DEV_OUT_OF_REACH: False} for name in s3.JUDGES}
    assert s3.gate2(_rows(qids), one_off, s1v)["passed"] is True
    lost = {name: {**every[name], "a": False} for name in s3.JUDGES}
    got = s3.gate2(_rows(qids), lost, s1v)
    assert got["passed"] is False and got["standard"]["regressed_vs_s1"] == ["a"]
    unjudged = {name: {"a": True, s3.DEV_OUT_OF_REACH: True} for name in s3.JUDGES}
    got = s3.gate2(_rows(qids), unjudged, s1v)
    assert got["passed"] is False
    assert got["record"]["abstentions"] == {"n": 1, "correct": 0}
    record_only = {**every, "record": {**every["record"], "a": False}}
    assert s3.gate2(_rows(qids), record_only, s1v)["passed"] is False


def test_gate3_counts_each_judges_target() -> None:
    rows = _rows(["a", "b", "c"])
    v = {
        "record": {"a": True, "b": True},
        "standard": {"a": True, "b": False, "c": True},
    }
    got = s3.gate3(rows, v)
    assert got["record"]["correct"] == 2 and got["record"]["misses"] == ["c"]
    assert got["standard"]["correct"] == 2 and got["standard"]["misses"] == ["b"]


# ---------------------------------------------------------------- the report


def test_gate4_needs_a_strict_lead_and_the_whole_f1_sample() -> None:
    rows = _rows(["a", "b", "c", "d"])
    target = {name: {"correct": 2} for name in s3.JUDGES}
    sample = ["c", "d"]
    every = {name: dict.fromkeys("abcd", True) for name in s3.JUDGES}
    got = s3.gate4(rows, every, target, sample)
    assert got["passed"] is True and got["standard"]["lead"] == 2
    sample_miss = {name: {**every[name], "d": False} for name in s3.JUDGES}
    got = s3.gate4(rows, sample_miss, target, sample)
    assert got["passed"] is False and got["record"]["f1_sample"]["correct"] == 1
    tied = {name: {"correct": 4} for name in s3.JUDGES}
    assert s3.gate4(rows, every, tied, sample)["passed"] is False  # a tie is a loss
    unjudged = {**every, "standard": {**every["standard"], "a": None}}
    got = s3.gate4(rows, unjudged, {n: {"correct": 3} for n in s3.JUDGES}, sample)
    assert got["record"]["passed"] is True and got["standard"]["passed"] is False
    assert got["standard"]["misses"] == ["a"] and got["passed"] is False


def test_the_outcome_predictions_follow_their_declared_bounds() -> None:
    def graded(**n: Any) -> dict[str, str]:
        base = {
            "dev": {"record": 150, "standard": 150},
            "dev_regressed": [],
            "target": {"record": 495, "standard": 495},
            "bettermemory": {"record": 498, "standard": 498},
            "lead": {"record": 3, "standard": 3},
            "f1_sample": {"record": 150, "standard": 150},
            "f1_sample_n": 150,
            "holdout_covered": 336,
            "holdout_labelled": 336,
            "reader_tokens": 70_000_000,
            "judge_usd": 1.5,
        }
        return {p["id"]: p["verdict"] for p in s3.outcome_predictions({**base, **n})}

    assert set(graded().values()) == {"HIT"}
    assert graded(dev={"record": 149, "standard": 150})["S3-P4"] == "HIT"
    assert graded(dev={"record": 150, "standard": 148})["S3-P4"] == "MISSED"
    assert graded(dev_regressed=["q"])["S3-P4"] == "MISSED"
    assert graded(target={"record": 498, "standard": 495})["S3-P5"] == "MISSED"
    assert graded(target={"record": 494, "standard": 495})["S3-P5"] not in (
        "HIT",
        "MISSED",
    )
    assert graded(bettermemory={"record": 500, "standard": 499})["S3-P6"] == "HIT"
    assert graded(bettermemory={"record": 497, "standard": 498})["S3-P6"] not in (
        "HIT",
        "MISSED",
    )
    assert graded(bettermemory={"record": 498, "standard": 496})["S3-P6"] == "MISSED"
    assert graded(lead={"record": 1, "standard": 0})["S3-P7"] == "MISSED"
    assert graded(f1_sample={"record": 150, "standard": 149})["S3-P8"] == "MISSED"
    assert graded(holdout_covered=335)["S3-P9"] not in ("HIT", "MISSED")
    assert graded(holdout_covered=334)["S3-P9"] == "MISSED"
    assert graded(reader_tokens=80_000_001)["S3-P10"] == "MISSED"
    assert graded(judge_usd=2.01)["S3-P10"] == "MISSED"


def test_flagged_names_the_questions_of_unclean_readers(tmp_path: Path) -> None:
    summary = {"summary": {"not_clean": ["batch_01"]}}
    (tmp_path / "audit-readers.json").write_text(json.dumps(summary), encoding="utf-8")
    meta = {"batches": [["q0"], ["q1"], ["q2"]]}
    assert s3.flagged(tmp_path, meta) == ["q1"]
