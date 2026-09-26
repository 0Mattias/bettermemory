"""Tests for the decision-layer harness, `bench/decisions/`.

The harness compares decision systems on the store's own workload. Three
things are pinned here: the Jev instrument's request and answer mapping
and its spend guard (offline, no network), the blind export leaking no
label, id or session, and the committed summary being the fold of the
committed per-arm artifacts.
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
_HERE = _ROOT / "bench" / "decisions"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"bench_decisions_{name}", _HERE / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"bench_decisions_{name}"] = module
    spec.loader.exec_module(module)
    return module


instruments = _load("instruments")
blind = _load("blind")
map_chat = _load("map_chat")
compare = _load("compare")
recall_dataset = _load("recall_dataset")

QUESTIONS: dict[str, Any] = {
    "contradicts": {
        "type": "noul",
        "instructions": "The new statement gives a different value than a stored statement.",
        "criteria": {"true": "It contradicts.", "false": "It does not."},
    },
    "relation": {
        "type": "choice",
        "instructions": "How does the new statement relate to the stored statements?",
        "criteria": {
            "update": "changed",
            "conflict": "differs",
            "restates": "same",
            "unrelated": "other",
        },
    },
}
# the shape OpenRouter's TypeSafe-compatible endpoint returned on the fictional probe of 2026-09-25
JEV_REPLY: dict[str, Any] = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "contradicts": {"type": "noul", "noul": 0.08},
        "relation": {
            "type": "choice",
            "choice": "update",
            "probabilities": {
                "unrelated": 0,
                "conflict": 0.05,
                "update": 0.95,
                "restates": 0,
            },
            "confidence": 0.93,
        },
    },
    "usage": {"input_tokens": 505, "output_tokens": 67, "cost": 2.121e-05},
    "id": "gen-dec-test",
    "provider": "TypeSafe",
}


def test_jev_needs_a_key_and_never_reads_it_from_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        instruments.Jev()


def test_jev_request_and_answer_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen: list[Any] = []

    def fake_post(req: Any, timeout: int = 120, attempts: int = 3) -> dict[str, Any]:
        seen.append(req)
        return json.loads(json.dumps(JEV_REPLY))

    monkeypatch.setattr(instruments, "post_json", fake_post)
    jev = instruments.Jev(max_cost=0.5)
    out = jev.ask("Stored statements:\n1. x\n\nNew statement:\ny", QUESTIONS)
    assert out == {
        "contradicts": {"noul": 0.08},
        "relation": {
            "choice": "update",
            "probabilities": {
                "unrelated": 0.0,
                "conflict": 0.05,
                "update": 0.95,
                "restates": 0.0,
            },
        },
    }
    req = seen[0]
    assert req.full_url == instruments.JEV_URL
    body = json.loads(req.data.decode())
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"].startswith("Stored statements:")
    assert body["questions"] == QUESTIONS
    assert req.get_header("Authorization") == "Bearer sk-or-test"
    assert jev.usage == {
        "calls": 1,
        "input_tokens": 505,
        "output_tokens": 67,
        "cost": 2.121e-05,
    }
    assert jev.version["served"] == "typesafe/jev-1.13-20260917"
    assert jev.version["provider"] == "TypeSafe"
    assert "sk-or-test" not in json.dumps(jev.version) + json.dumps(jev.usage)


def test_jev_spend_guard_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    reply = json.loads(json.dumps(JEV_REPLY))
    reply["usage"]["cost"] = 0.3
    monkeypatch.setattr(
        instruments,
        "post_json",
        lambda req, timeout=120, attempts=3: json.loads(json.dumps(reply)),
    )
    jev = instruments.Jev(max_cost=0.5)
    jev.ask("s", QUESTIONS)
    jev.ask("s", QUESTIONS)  # 0.6 spent after this one
    with pytest.raises(RuntimeError, match="spend guard"):
        jev.ask("s", QUESTIONS)
    assert jev.usage["calls"] == 2


def test_estimate_is_linear_in_characters() -> None:
    est = instruments.estimate_jev_cost(["a" * 1000], [QUESTIONS])
    chars = 1000 + len(json.dumps(QUESTIONS, ensure_ascii=False))
    assert est["calls"] == 1 and est["chars"] == chars
    assert est["tokens_estimated"] == int(chars * instruments.JEV_TOKENS_PER_CHAR)
    assert est["usd_estimated"] == round(
        est["tokens_estimated"] * instruments.JEV_USD_PER_INPUT_TOKEN, 4
    )
    two = instruments.estimate_jev_cost(
        ["a" * 1000, "b" * 1000], [QUESTIONS, QUESTIONS]
    )
    assert two["chars"] == 2 * chars


def test_blind_export_carries_no_label_id_or_session() -> None:
    rows = [
        {
            "i": i,
            "id": f"01MEMORY{i:021d}",
            "state": f"User message:\nprompt {i}",
            "label": i % 2,
            "relevance": "high",
            "rank": 0,
            "score": 1.0,
        }
        for i in range(7)
    ]
    chunks, mapping = blind.export(rows, "recall", chunk=3, seed=5)
    assert [len(c) for c in chunks] == [3, 3, 1]
    flat = [r for c in chunks for r in c]
    assert {tuple(sorted(r)) for r in flat} == {("case", "state")}
    assert sorted(mapping.values()) == list(range(7))
    assert all(len(oid) == 10 and oid.startswith("c") for oid in mapping)
    assert [r["case"] for r in flat] != [
        blind.opaque(5, str(i)) for i in range(7)
    ]  # shuffled
    text = json.dumps(flat)
    assert "01MEMORY" not in text and "label" not in text
    answers = {r["case"]: 0.1 * mapping[r["case"]] for r in flat}
    probs = map_chat.recall_answers(mapping, answers)
    assert probs == pytest.approx([0.1 * i for i in range(7)])


def test_blind_integrity_export_keeps_the_questions() -> None:
    rows = [
        {"stmt_id": f"t{i}.f1", "state": "s", "questions": QUESTIONS} for i in range(2)
    ]
    chunks, mapping = blind.export(rows, "integrity", chunk=10, seed=1)
    assert {tuple(sorted(r)) for r in chunks[0]} == {("case", "questions", "state")}
    assert sorted(mapping.values()) == ["t0.f1", "t1.f1"]
    judged = {
        oid: {
            "contradicts": 0.9,
            "instruction": 0.1,
            "secret": 0.0,
            "relation": {"update": 3, "conflict": 1},
        }
        for oid in mapping
    }
    out = map_chat.integrity_answers(mapping, judged)
    assert out["t0.f1"]["contradicts"] == {"noul": 0.9}
    assert out["t0.f1"]["relation"] == {
        "choice": "update",
        "probabilities": {"update": 0.75, "conflict": 0.25},
    }


def _transcript(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_recall_dataset_cutoff_drops_later_rows(tmp_path: Path) -> None:
    store = {
        "01ARZ3NDEKTSV4RRFFQ69G5FAV": {
            "body": "the memory body",
            "scopes": ["tools"],
            "tombstoned": False,
        }
    }
    hit = {
        "id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "score": 0.5,
        "relevance": "high",
        "snippet": "snip",
    }

    def search(ts: str, tool_id: str, query: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "user",
                "timestamp": ts,
                "message": {"content": "please look up " + query},
            },
            {
                "type": "assistant",
                "timestamp": ts,
                "message": {
                    "model": "m",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "mcp__bettermemory__memory_search",
                            "input": {"query": query},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": ts,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps({"result": [hit]}),
                        }
                    ]
                },
            },
        ]

    rows = search("2026-09-25T19:00:00Z", "t1", "early") + search(
        "2026-09-25T20:00:00Z", "t2", "late"
    )
    t = tmp_path / "proj" / "session.jsonl"
    t.parent.mkdir()
    _transcript(t, rows)
    hits_all, _, _, _ = recall_dataset.build([str(t)], store, None)
    assert [h["query"] for h in hits_all] == ["early", "late"]
    hits_cut, _, stats, _ = recall_dataset.build(
        [str(t)], store, recall_dataset.parse_ts("2026-09-25T19:30:00Z")
    )
    assert [h["query"] for h in hits_cut] == ["early"]
    assert stats["rows_after_cutoff"] == 3
    assert (
        hits_cut[0]["body"] == "the memory body"
        and hits_cut[0]["prompt"] == "please look up early"
    )


def test_committed_summary_is_the_fold_of_the_committed_arms() -> None:
    summary = json.loads((_HERE / "results" / "summary.json").read_text())
    assert summary["integrity"]["arms"] == compare.integrity_rows()
    assert summary["recall"]["arms"] == compare.recall_rows()
    assert summary["integrity"]["baseline"] == compare.BASE_INTEGRITY
    names = {r["instrument"] for r in summary["integrity"]["arms"]}
    assert "claude-fable-5.1-in-session" in names


retest = _load("retest")


def test_retest_statistics_on_identical_and_reversed_judgments() -> None:
    old = [0.1, 0.4, 0.6, 0.9, 0.3, 0.8]
    labels = [0, 0, 1, 1, 0, 1]
    same = retest.compare(old, list(old), labels)
    assert same["pearson"] == 1.0 and same["spearman"] == 1.0
    assert same["same_side_of_0.5"] == 1.0 and same["mean_abs_diff"] == 0.0
    assert same["auc_old"] == 1.0 and same["auc_new_same_cases"] == 1.0
    flipped = retest.compare(old, [1 - x for x in old], labels)
    assert flipped["pearson"] == -1.0 and flipped["spearman"] == -1.0
    assert flipped["same_side_of_0.5"] == 0.0
    assert flipped["auc_new_same_cases"] == 0.0


def test_retest_instance_rows_follow_the_driver() -> None:
    rows: list[dict[str, Any]] = [
        {"opened": True, "explicit": None, "prompt": "p", "body": "b"},
        {"opened": False, "explicit": "ignored", "prompt": "p", "body": "b"},
        {
            "opened": False,
            "explicit": None,
            "prompt": "p",
            "body": "b",
        },  # unlabelled: dropped
        {
            "opened": True,
            "explicit": None,
            "prompt": None,
            "body": "b",
        },  # no prompt: dropped
    ]
    kept = retest.instance_rows([dict(r) for r in rows])
    assert sorted(r["label"] for r in kept) == [0, 1]
