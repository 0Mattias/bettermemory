"""Tests for the Agent Memory Leaderboard adapter and its local harness
(bench/aml, bench/judge/prompts.py, bench/llm.py).

The adapter is what AML scores, so the cases are the ones that would
corrupt a submission silently rather than crash it:

- a retried Add (same request_id) writing its rounds twice
- one user_id's memories reaching another user_id's Search
- the Add response not echoing the request's ids byte for byte
- an unauthenticated request being served
- the trim cutting the user's own words, or a top-ranked round
- the presentation levers reordering or padding the wrong way
- the judge prompts drifting from their published sources by a character
- the dev/holdout split moving between runs

Everything is hermetic: stores live under `tmp_path`, and nothing here
calls a model.
"""

from __future__ import annotations

import hashlib
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


service = _load("aml.service", _BENCH / "aml" / "service.py")
fts = _load("aml.fts", _BENCH / "aml" / "fts.py")
server = _load("aml.server", _BENCH / "aml" / "server.py")
prompts = _load("judge.prompts", _BENCH / "judge" / "prompts.py")
runner = _load("bench_aml_run", _BENCH / "aml" / "run.py")

DAY = 86_400_000
T0 = 1_684_584_000_000  # 2023-05-20 12:00 UTC, a Saturday


def _msgs(*pairs: tuple[str, str], ts: int = T0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for user, assistant in pairs:
        out.append({"role": "user", "content": user, "timestamp": ts})
        if assistant:
            out.append({"role": "assistant", "content": assistant, "timestamp": ts})
    return out


# ---------------------------------------------------------------- ingest


def test_rounds_pair_user_with_following_assistant_and_keep_a_trailing_turn() -> None:
    rounds = service.rounds_of(
        [
            {"role": "user", "content": "a", "timestamp": T0},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c", "timestamp": T0 + 1},
        ]
    )
    assert rounds == [("user: a\nassistant: b", T0), ("user: c", T0 + 1)]


def test_a_retried_add_writes_nothing_the_second_time(tmp_path: Path) -> None:
    svc = service.MemoryService(tmp_path)
    msgs = _msgs(("I adopted a beagle named Biscuit.", "Great name!"))
    assert svc.add("req-1", "u1", msgs, "s1") == 1
    assert svc.add("req-1", "u1", msgs, "s1") == 0
    fresh = service.MemoryService(tmp_path)  # the record survives a restart
    assert fresh.add("req-1", "u1", msgs, "s1") == 0
    assert len(fresh.search("u1", "beagle Biscuit", 100)) == 1


def test_search_never_reads_another_user_id(tmp_path: Path) -> None:
    svc = service.MemoryService(tmp_path)
    svc.add("r1", "alice", _msgs(("My beagle is named Biscuit.", "")), "s")
    svc.add("r2", "bob", _msgs(("I drive a red Volvo.", "")), "s")
    assert svc.search("bob", "beagle Biscuit", 100) == []
    assert [h["content"] for h in svc.search("alice", "Volvo", 100)] == []


def test_served_rounds_carry_the_source_date_header_and_created_at(
    tmp_path: Path,
) -> None:
    svc = service.MemoryService(tmp_path)
    svc.add("r", "u", _msgs(("My new Samsung TV is 55 inches.", "Nice.")), "s")
    [hit] = svc.search("u", "What size is my Samsung TV?", 100)
    assert hit["content"].startswith("[2023/05/20 (Sat) 12:00]\nuser: My new Samsung")
    assert hit["created_at"] == "2023-05-20T12:00:00+00:00"


# ---------------------------------------------------------------- levers


def _three_session_service(tmp_path: Path, **kw: Any) -> Any:
    svc = service.MemoryService(tmp_path, **kw)
    svc.add(
        "r1",
        "u",
        _msgs(
            ("We talked about the garden first.", "ok"),
            ("Then I planted tomatoes in the garden.", "ok"),
            ("Weather was cold.", "ok"),
        ),
        "s1",
    )
    svc.add(
        "r2", "u", _msgs(("Tomatoes again, now staked.", "ok"), ts=T0 + 3 * DAY), "s2"
    )
    return svc


def test_neighbor_fill_pads_with_same_session_rounds_next_to_a_hit(
    tmp_path: Path,
) -> None:
    plain = _three_session_service(tmp_path / "a")
    filled = _three_session_service(tmp_path / "b", fill="neighbors")
    hits = [h["content"] for h in plain.search("u", "tomatoes", 100)]
    padded = [h["content"] for h in filled.search("u", "tomatoes", 100)]
    assert padded[: len(hits)] == hits  # engine order kept for the scored rounds
    extra = padded[len(hits) :]
    assert any("garden first" in c for c in extra)
    assert any("Weather was cold" in c for c in extra)


def test_chronological_order_sorts_by_source_time(tmp_path: Path) -> None:
    svc = _three_session_service(tmp_path, order="chronological")
    served = [h["created_at"] for h in svc.search("u", "tomatoes garden", 100)]
    assert served == sorted(served)


def test_serve_caps_the_result_count_below_top_k(tmp_path: Path) -> None:
    svc = _three_session_service(tmp_path, serve=1)
    assert len(svc.search("u", "tomatoes garden", 100)) == 1


def test_age_annotation_counts_days_to_the_newest_conversation(tmp_path: Path) -> None:
    svc = _three_session_service(tmp_path, annotate="age")
    heads = {h["content"].split("\n")[0] for h in svc.search("u", "tomatoes", 100)}
    assert (
        "[2023/05/20 (Sat) 12:00; 3 days before the most recent conversation "
        "on 2023/05/23 (Tue)]" in heads
    )
    assert (
        "[2023/05/23 (Tue) 12:00; the same day as the most recent conversation]"
        in heads
    )


def test_trim_keeps_user_words_and_matching_assistant_lines() -> None:
    body = (
        "[2023/05/20 (Sat) 12:00]\n"
        "user: What were the jobs for seniors?\n"
        "assistant: Here is a list.\n"
        "1. Virtual assistant\n"
        "2. Tutoring work for seniors\n"
        "3. Pet sitting"
    )
    out = service.trim_assistant(body, service.query_terms("jobs for seniors tutoring"))
    assert out.split("\n") == [
        "[2023/05/20 (Sat) 12:00]",
        "user: What were the jobs for seniors?",
        "assistant: Here is a list.",
        "[...]",
        "2. Tutoring work for seniors",
        "[...]",
    ]


def test_tail_trim_leaves_the_top_ranked_rounds_whole(tmp_path: Path) -> None:
    svc = service.MemoryService(tmp_path, trim="tail")
    long_reply = "Sure.\nunrelated line one\nunrelated line two"
    svc.add(
        "r", "u", _msgs(*[(f"tomato note {i}", long_reply) for i in range(12)]), "s"
    )
    served = svc.search("u", "tomato note", 100)
    assert len(served) == 12
    assert all("unrelated line one" in h["content"] for h in served[:10])
    assert all("unrelated line one" not in h["content"] for h in served[10:])


def test_unknown_lever_values_are_refused(tmp_path: Path) -> None:
    for kw in ({"fill": "x"}, {"order": "x"}, {"annotate": "x"}, {"trim": "x"}):
        with pytest.raises(ValueError):
            service.MemoryService(tmp_path, **kw)


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def client(tmp_path: Path) -> Any:
    from starlette.testclient import TestClient

    return TestClient(server.build_app(service.MemoryService(tmp_path), "t0k"))


_ADD = {
    "request_id": "eval:run:locomo:conv-0:chunk-0",
    "messages": [
        {
            "role": "user",
            "timestamp": T0,
            "content": "I adopted a beagle named Biscuit.",
        },
        {"role": "assistant", "content": "Biscuit is a great name!"},
    ],
    "user_id": "eval:run:locomo:conv-0",
    "session_id": "eval:run:sample:0",
}


def test_health_is_unauthenticated(client: Any) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_add_and_search_refuse_a_missing_or_wrong_token(client: Any) -> None:
    assert client.post("/add", json=_ADD).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/add", json=_ADD, headers=bad).status_code == 401
    body = {"query": "q", "user_id": "u", "top_k": 5}
    assert client.post("/search", json=body).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer t0k"},
        {"Authorization": "Token t0k"},
        {"X-Api-Key": "t0k"},
    ],
)
def test_add_echoes_the_request_ids_exactly(
    client: Any, headers: dict[str, str]
) -> None:
    resp = client.post("/add", json=_ADD, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "request_id": _ADD["request_id"],
        "user_id": _ADD["user_id"],
        "session_id": _ADD["session_id"],
    }


def test_search_returns_rank_ordered_data_and_uses_options(client: Any) -> None:
    auth = {"X-Api-Key": "t0k"}
    client.post("/add", json=_ADD, headers=auth)
    plain = client.post(
        "/search",
        json={
            "query": "What pet did I adopt?",
            "user_id": _ADD["user_id"],
            "top_k": 100,
        },
        headers=auth,
    ).json()["data"]
    assert plain == []  # no shared content word with the stored round
    with_options = client.post(
        "/search",
        json={
            "query": "What pet did I adopt?",
            "options": ["A. a beagle", "B. a parrot"],
            "user_id": _ADD["user_id"],
            "top_k": 100,
        },
        headers=auth,
    ).json()["data"]
    assert [set(h) for h in with_options] == [{"id", "content", "score", "created_at"}]


# ---------------------------------------------------------------- calibration arm


def test_fts_baseline_is_idempotent_isolated_and_ranks_matches(tmp_path: Path) -> None:
    svc = fts.FtsService(tmp_path)
    msgs = _msgs(
        ("My beagle is named Biscuit.", "Cute."), ("I drive a Volvo.", "Nice.")
    )
    assert svc.add("r", "u", msgs, "s1") == 2
    assert svc.add("r", "u", msgs, "s1") == 0
    hits = svc.search("u", "What is my beagle called?", 100)
    assert len(hits) == 1 and "Biscuit" in hits[0]["content"]
    assert svc.sessions_for("u", [h["id"] for h in hits]) == ["s1"]
    assert svc.search("other", "beagle", 100) == []


# ---------------------------------------------------------------- judge prompts


def test_judge_prompts_match_their_published_sources_exactly() -> None:
    """Pinned by hash: a harness grading with an edited prompt is grading a
    different benchmark. Re-pin only when the upstream source changes, and
    say which commit in the message."""
    aml = hashlib.sha256(prompts.AML_ACCURACY_PROMPT.encode("utf-8")).hexdigest()
    assert aml == "44b751660e4e0950ee640b14207a0ab7d519c4558374d429b6bf262d9871d6ff"
    lme = "\n".join(
        [
            prompts.lme_prompt(t, "Q", "A", "R", False)
            for t in (
                "single-session-user",
                "temporal-reasoning",
                "knowledge-update",
                "single-session-preference",
            )
        ]
        + [prompts.lme_prompt("multi-session", "Q", "A", "R", True)]
    )
    assert hashlib.sha256(lme.encode("utf-8")).hexdigest() == (
        "bf7e389b9c0c43a4412ad80bca34e0de7d9cca0dd1a12a863dcfa63f8056d436"
    )


def test_judge_parse_rules_follow_their_sources() -> None:
    assert prompts.parse_lme("Yes.") is True
    assert prompts.parse_lme("no") is False
    assert prompts.parse_aml('reason.\n```json\n{"label": "CORRECT"}\n```') is True
    assert prompts.parse_aml('{"label": "WRONG"}') is False
    assert prompts.parse_aml("CORRECT") is None
    assert prompts.parse_aml('{"label": "MAYBE"}') is None


# ---------------------------------------------------------------- split


def test_dev_split_is_fixed_stratified_and_disjoint() -> None:
    types = ["a"] * 200 + ["b"] * 150 + ["c"] * 100 + ["d"] * 50
    corpus = [{"question_id": f"q{i}", "question_type": t} for i, t in enumerate(types)]
    dev, holdout = runner.split(corpus)
    again, _ = runner.split(list(reversed(corpus)))
    assert dev == again
    assert len(dev) == 150 and len(holdout) == 350
    assert not set(dev) & set(holdout)
    by_type = {
        t: sum(1 for q in corpus if q["question_id"] in dev and q["question_type"] == t)
        for t in "abcd"
    }
    assert by_type == {"a": 60, "b": 45, "c": 30, "d": 15}


def test_llm_cache_key_depends_on_every_sampling_parameter() -> None:
    llm = _load("llm", _BENCH / "llm.py")
    base = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 5,
        "temperature": 0.0,
    }
    keys = {
        llm.Client.cache_key(base),
        llm.Client.cache_key({**base, "model": "n"}),
        llm.Client.cache_key({**base, "temperature": 0.3}),
        llm.Client.cache_key({**base, "reasoning": {"effort": "low", "exclude": True}}),
    }
    assert len(keys) == 4
    assert llm.Client.cache_key(
        dict(reversed(list(base.items())))
    ) == llm.Client.cache_key(base)
    assert json.dumps(base)  # payloads are plain JSON
