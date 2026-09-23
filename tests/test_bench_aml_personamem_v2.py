"""Tests for the PersonaMem v2 cell of the Agent Memory Leaderboard
reproduction (bench/aml/ds_personamem_v2.py).

The cases are the ones that would corrupt the cell silently:

- a prompt or parse rule drifting from AML's pipeline by a character
- the option order moving between runs, or away from the pipeline's
  seeded shuffle (the pipeline seeds from `hash()`, salted per process)
- a letter the pipeline would score wrong being scored right, or the
  reverse
- one persona's history reaching another persona's store, or the persona
  prompt shifting the user/assistant rounds after it
- the recall sentence leaking into the Search query

Everything is hermetic: the data is a synthetic fixture under
`tmp_path`, and nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
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


pm = _load("aml.ds_personamem_v2", _BENCH / "aml" / "ds_personamem_v2.py")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- prompts


def test_prompts_and_parse_rules_match_the_published_pipeline_exactly() -> None:
    """Pinned by hash against AML data/personamem/pipeline_v2.py (commit
    1b8142b), each digest taken from the `ast`-extracted source value.
    Re-pin only when that source changes, and say which commit."""
    pins = {
        "RECALL_SUFFIX": "ae790c38a31a4136ffe0b8aa1f8a441666d18a010dc1b191a33d923093e15f2a",
        "MCQ_PROMPT_TEMPLATE": "075d4aadf336f9af0ada074f14c7a70882f419c37d9c0b90ef7652a6477d8950",
        "JUDGE_PROMPT_NARROW_POSITIVE": "f462671eceeb4ef07610b3e13afc8312c6622def032539062951b584375e87b5",
        "JUDGE_PROMPT_NARROW_NEGATIVE": "1f0d8e0276fd4059e2364fb1da57ce89a12550629933901f96aaa84493427a0f",
        "LETTER_PATTERNS": "031eacd6629234ca371b1b8b770515db981b8e9bd6d31d6df774fc9140346a39",
        "BOXED_PATTERNS": "635eedf0f5641f56b6f0cf384388db4ab7172e0921f2e1b634a4f7a9818a7da1",
        "SCORE_PATTERNS": "c6f1f5e652e640c1d1980c11b906deec0af2ebdadf9c6aa10b9a2699163926e4",
    }
    for name, digest in pins.items():
        value = getattr(pm, name)
        text = value if isinstance(value, str) else "\n".join(value)
        assert _sha(text) == digest, name


def test_the_judge_takes_the_negative_prompt_for_a_do_not_preference() -> None:
    neg = pm.narrow_judge_prompt("Q?", "Do not remember 'X' in memory", "R")
    pos = pm.narrow_judge_prompt("Q?", "Loves hiking", "R")
    assert '"do not personalize"' in neg and '"do not personalize"' not in pos
    assert pos.startswith(pm.JUDGE_PROMPT_NARROW_POSITIVE.split("{user_query}")[0])
    assert "User's query:\nQ?\n" in pos and "{" not in pos.replace("\\boxed{score}", "")
    assert pos.rstrip().endswith(
        "\\boxed{score} where score is a number between 0.0 and 1.0.\nYour response:"
    )


# ---------------------------------------------------------------- options


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a",
        "abcdefg",
        "abcdefgh",
        "abcdefghi",
        "héllo",
        "em—dash “q”",
        "x 😀",
        "z" * 300,
    ],
)
def test_str_hash_is_cpython_hash_with_the_salt_off(text: str) -> None:
    """The pipeline seeds its shuffle with `hash()`; this is that value as
    CPython computes it under PYTHONHASHSEED=0, for every string width."""
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print(hash(sys.stdin.read()))"],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"},
        check=True,
    ).stdout
    assert pm.str_hash(text) == int(out)


def test_a_local_rng_shuffles_exactly_as_the_seeded_global_one() -> None:
    options = ["right", "w1", "w2", "w3"]
    for seed in (0, 1, 2**31 + 7, 2**32 - 1):
        local = list(options)
        random.Random(seed).shuffle(local)
        state = random.getstate()
        try:
            glob = list(options)
            random.seed(seed)
            random.shuffle(glob)
        finally:
            random.setstate(state)
        assert local == glob


def _item(persona: str, query: str, wrong: list[str]) -> dict[str, Any]:
    return {
        "persona_id": persona,
        "user_query": repr({"role": "user", "content": query}),
        "correct_answer": "right",
        "incorrect_answers": json.dumps(wrong),
    }


def test_option_order_is_the_pipelines_and_the_same_in_every_process() -> None:
    """The expected orders are what AML's own official_mcq_options returns
    under PYTHONHASHSEED=0 for these items."""
    a = _item(
        "7", "Any ideas for a weekend trip?", ["wrong one", "wrong two", "wrong three"]
    )
    b = _item(
        "12", "What should I cook tonight — something “quick”?", ["w1", "w2", "w3"]
    )
    assert pm.official_mcq_options(a) == (
        ["wrong one", "wrong three", "wrong two", "right"],
        {"A": "wrong one", "B": "wrong three", "C": "wrong two", "D": "right"},
        "D",
    )
    assert pm.official_mcq_options(b)[0] == ["w2", "w1", "w3", "right"]
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "import importlib.util as u;"
        "s = u.spec_from_file_location('m', sys.argv[2]); m = u.module_from_spec(s);"
        "s.loader.exec_module(m);"
        "print(json.dumps(m.official_mcq_options(json.loads(sys.stdin.read()))))"
    )
    for salt in ("1", "4242"):
        out = subprocess.run(
            [sys.executable, "-c", code, str(_BENCH), str(pm.__file__)],
            input=json.dumps(b),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONHASHSEED": salt, "PYTHONIOENCODING": "utf-8"},
            check=True,
        ).stdout
        assert json.loads(out) == json.loads(json.dumps(pm.official_mcq_options(b)))


def test_mcq_mode_refuses_an_item_without_wrong_answers() -> None:
    with pytest.raises(TypeError):
        pm.official_mcq_options(_item("1", "q", []))


# ---------------------------------------------------------------- parsers


@pytest.mark.parametrize(
    ("reply", "letter"),
    [
        ("Reasoning...\nFinal Answer: B", "B"),
        ("so $\\boxed{C}$", "C"),
        ("I think the answer is d", "D"),
        ("**Final Answer: A**", "A"),
        ("It must be option C.", "C"),
        # The pipeline's rules miss a bracketed letter, the template's own
        # "[Letter]" shape; AML scores such a reply wrong, and so must this.
        ("Final Answer: [B]", ""),
        ("I cannot tell from the conversation", ""),
        ("", ""),
    ],
)
def test_the_letter_parse_follows_the_pipeline(reply: str, letter: str) -> None:
    assert pm.extract_final_letter(reply) == letter


@pytest.mark.parametrize(
    ("reply", "score"),
    [
        ("Well aligned. \\boxed{0.8}", 0.8),
        ("\\boxed{1.5}", 1.0),
        ("$\\boxed{0}$", 0.0),
        ("Final score: 0.7", 0.7),
        ("I'd give it 0.6 / 1.0", 0.6),
        ("score: 7", None),  # out of range, and no other rule matches
        ("The response is generic.", None),
        ("", None),
    ],
)
def test_the_score_parse_follows_the_pipeline(reply: str, score: float | None) -> None:
    """None is exactly where the pipeline falls through to its 0.0 default."""
    assert pm.parse_score(reply) == score


# ---------------------------------------------------------------- loading


def _write_fixture(root: Path) -> None:
    hist = root / "data" / "chat_history_128k"
    hist.mkdir(parents=True)
    h1 = [
        {
            "role": "system",
            "content": "You are an AI assistant helping a user with the following persona: P1",
        },
        {"role": "user", "content": "Polish my email: I'm allergic to pollen."},
        {"role": "assistant", "content": "Here it is."},
        {"role": "user", "content": "Solve 2+2."},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "Translate: I love hiking."},
        {"role": "assistant", "content": "Ok", "stray": "rest of the reply"},
    ]
    h2 = [
        {"role": "system", "content": "persona P2"},
        {"role": "user", "content": "My dog is called Rex."},
        {"role": "assistant", "content": "Nice."},
    ]
    (hist / "p1.json").write_text(
        json.dumps({"metadata": {}, "chat_history": h1}), encoding="utf-8"
    )
    (hist / "p2.json").write_text(
        json.dumps({"metadata": {}, "chat_history": h2}), encoding="utf-8"
    )
    snippet = json.dumps(h1[1:3])
    missing = json.dumps([{"role": "user", "content": "My SSN is 123."}])
    rows = [
        (
            "1",
            "p1.json",
            "How to keep my home air fresh?",
            "health_and_medical_conditions",
            snippet,
        ),
        (
            "1",
            "p1.json",
            "My SSN is 123, help me file a claim.",
            "sensitive_info",
            missing,
        ),
        (
            "2",
            "p2.json",
            "Any pet toy ideas?",
            "neutral_preferences",
            json.dumps(h2[1:3]),
        ),
    ]
    bench = root / "benchmark" / "text"
    bench.mkdir(parents=True)
    with (bench / "benchmark.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "persona_id",
                "chat_history_128k_link",
                "user_query",
                "correct_answer",
                "incorrect_answers",
                "preference",
                "pref_type",
                "related_conversation_snippet",
            ],
        )
        w.writeheader()
        for persona, link, query, ptype, snip in rows:
            w.writerow(
                {
                    "persona_id": persona,
                    "chat_history_128k_link": f"data/chat_history_128k/{link}",
                    "user_query": repr({"role": "user", "content": query}),
                    "correct_answer": f"right for {query}",
                    "incorrect_answers": json.dumps(["w1", "w2", "w3"]),
                    "preference": "Seasonal pollen allergy",
                    "pref_type": ptype,
                    "related_conversation_snippet": snip,
                }
            )


@pytest.fixture
def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    _write_fixture(tmp_path)
    monkeypatch.setattr(pm, "DATA", tmp_path)
    return pm.load()


def test_one_store_per_persona_with_the_persona_prompt_on_its_own(loaded: Any) -> None:
    questions, sessions = loaded
    assert sorted(sessions) == ["personamem-v2:1", "personamem-v2:2"]
    persona, history = sessions["personamem-v2:1"]
    assert persona["session_id"] == "persona"
    assert [m["role"] for m in persona["messages"]] == ["system"]
    assert history["session_id"] == "history"
    # the empty reply is dropped, the stray key is not sent, and the
    # rounds start on a user message
    assert history["messages"] == [
        {"role": "user", "content": "Polish my email: I'm allergic to pollen."},
        {"role": "assistant", "content": "Here it is."},
        {"role": "user", "content": "Solve 2+2."},
        {"role": "user", "content": "Translate: I love hiking."},
        {"role": "assistant", "content": "Ok"},
    ]
    assert "Rex" not in json.dumps(sessions["personamem-v2:1"])
    assert [q["user_id"] for q in questions] == [
        "personamem-v2:1",
        "personamem-v2:1",
        "personamem-v2:2",
    ]


def test_questions_carry_the_query_the_options_and_the_evidence(loaded: Any) -> None:
    questions, _ = loaded
    q = questions[0]
    assert q["question"] == "How to keep my home air fresh?"  # no recall sentence
    assert q["question_type"] == "health_and_medical_conditions"
    assert q["answer_session_ids"] == []
    assert q["options"] == [f"{k}. {v}" for k, v in q["option_mapping"].items()]
    assert q["option_mapping"][q["correct_letter"]] == q["answer"]
    assert q["evidence_turns"] == {"turn-1": "Polish my email: I'm allergic to pollen."}
    assert questions[1]["evidence_turns"] == {}  # snippet not in the history


def test_generative_mode_sends_no_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture(tmp_path)
    monkeypatch.setattr(pm, "DATA", tmp_path)
    monkeypatch.setattr(pm, "MODE", "generative")
    questions, _ = pm.load()
    assert all("options" not in q for q in questions)


def test_a_persona_with_two_histories_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture(tmp_path)
    path = tmp_path / "benchmark" / "text" / "benchmark.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("2,data/chat_history_128k/p2", "1,data/chat_history_128k/p2"),
        encoding="utf-8",
    )
    monkeypatch.setattr(pm, "DATA", tmp_path)
    with pytest.raises(ValueError):
        pm.load()


# ---------------------------------------------------------------- grading


class _Client:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, model: str, messages: list[dict[str, str]], **kw: Any
    ) -> Any:
        self.calls.append({"model": model, "messages": messages, **kw})
        return SimpleNamespace(text=self.replies.pop(0), cost=0.001)


def test_mcq_answer_messages_and_credit(loaded: Any) -> None:
    q = loaded[0][0]
    hits = [
        {
            "content": "user: Polish my email: I'm allergic to pollen.\nassistant: Here it is."
        }
    ]
    client = _Client([f"Pollen matters.\nFinal Answer: {q['correct_letter']}"])
    row = asyncio.run(pm.answer_and_grade(client, q, hits, "reader", "judge", "off"))
    [call] = client.calls
    assert call["model"] == "reader" and call["temperature"] == 0.0
    assert call["max_tokens"] == pm.MAX_TOKENS
    msgs = call["messages"]
    assert msgs[0] == {"role": "user", "content": hits[0]["content"]}
    assert msgs[1] == {"role": "user", "content": q["question"] + pm.RECALL_SUFFIX}
    assert msgs[2]["role"] == "system"
    assert msgs[2]["content"] == pm.MCQ_PROMPT_TEMPLATE.format(
        options="\n".join(q["options"])
    )
    assert row["verdict"] is True
    assert row["evidence_sessions"] == ["turn-1"] and row["evidence_served"] == 1
    assert set(row) >= {
        "question_id",
        "question_type",
        "abstention",
        "n_hits",
        "prompt_chars",
        "generated",
        "verdict",
        "judge_raw",
        "evidence_sessions",
        "evidence_served",
        "cost",
    }
    wrong = next(k for k in q["option_mapping"] if k != q["correct_letter"])
    for reply in (
        f"Final Answer: {wrong}",
        "Final Answer: [" + q["correct_letter"] + "]",
    ):
        row = asyncio.run(pm.answer_and_grade(_Client([reply]), q, [], "r", "j", "off"))
        assert row["verdict"] is False and row["evidence_served"] == 0


def test_generative_judgment_is_retried_once_when_unparseable(
    loaded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pm, "MODE", "generative")
    q = loaded[0][0]
    client = _Client(
        ["Keep windows shut in pollen season.", "It is personalized", "\\boxed{0.9}"]
    )
    row = asyncio.run(pm.answer_and_grade(client, q, [], "reader", "judge", "low"))
    answer, first, retry = client.calls
    assert len(answer["messages"]) == 1  # no memories, no option block
    assert answer["messages"][0]["content"].endswith(pm.RECALL_SUFFIX)
    assert first["model"] == "judge" and first["max_tokens"] == pm.JUDGE_MAX_TOKENS
    assert retry["max_tokens"] == pm.JUDGE_RETRY_MAX_TOKENS
    assert first["reasoning_effort"] == "low" and first["messages"] == retry["messages"]
    assert row["score"] == 0.9 and row["verdict"] is True
    assert row["cost"] == pytest.approx(0.003)
    row = asyncio.run(
        pm.answer_and_grade(
            _Client(["a", "no score", "still none"]), q, [], "r", "j", "off"
        )
    )
    assert row["verdict"] is None and row["score"] == 0.0
