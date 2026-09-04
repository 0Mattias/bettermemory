"""Tests for the integrity benchmark's harness (bench/integrity).

The scorer is the benchmark: if it misclassifies a served item, or lets
a constant function score, every published rate is noise. So the cases
here are the ones a careless scorer gets backwards:

- an item carrying both values is current, not stale
- a signal every hit carries counts as nothing (the informative rule)
- the three references score exactly what the corpus construction says
- the shipped corpus passes its own sanity gates
- the bettermemory adapter, on a scratch store, labels a planted file
  `unaccounted` and a plant with a forged event line `local`

Everything is hermetic: the bettermemory arm runs in-process on a
`tmp_path` store; no rival package is imported.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BENCH = _ROOT / "bench" / "integrity"


def _load(name: str, filename: str) -> ModuleType:
    if str(_BENCH) not in sys.path:
        sys.path.insert(0, str(_BENCH))
    spec = importlib.util.spec_from_file_location(name, _BENCH / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = _load("score", "score.py")
adapters = _load("adapters", "adapters.py")
runner = _load("bench_integrity_run", "run.py")


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads((_BENCH / "corpus.json").read_text(encoding="utf-8"))


def _topic(kind: str = "supersession", current: str = "new") -> dict:
    return {
        "id": "tx",
        "kind": kind,
        "values": {"old": "deploy-gateway", "new": "release-gateway"},
        "current": current,
        "statements": [],
        "query": "which workflow?",
    }


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_containment_survives_paraphrase_punctuation() -> None:
    assert score.carries(
        "now runs through Release Gateway in Actions", "release-gateway"
    )
    assert score.carries("the `release_gateway` workflow", "release-gateway")
    assert not score.carries("the gateway release", "release-gateway")


def test_item_with_both_values_is_current() -> None:
    topic = _topic()
    assert (
        score.classify("renamed from deploy-gateway to release-gateway", topic)
        == "current"
    )
    assert score.classify("deploys go through deploy-gateway", topic) == "stale"
    assert score.classify("the canary soak is ten minutes", topic) == "other"


def test_reversion_topic_current_is_the_old_value() -> None:
    topic = _topic("reversion", current="old")
    assert score.classify("builds on deploy-gateway again", topic) == "current"
    assert score.classify("moved to release-gateway", topic) == "stale"


# ---------------------------------------------------------------------------
# the informative-signal rule
# ---------------------------------------------------------------------------


def _hits(*pairs: tuple[str, bool]) -> list[dict]:
    return [{"rank": i + 1, "text": t, "signal": s} for i, (t, s) in enumerate(pairs)]


def test_signal_every_hit_carries_counts_as_nothing() -> None:
    topic = _topic()
    out = score.topic_outcome(
        _hits(("release-gateway now", True), ("deploy-gateway before", True)), topic
    )
    assert out["stale_served"] and not out["stale_signaled"] and out["stale_unsignaled"]


def test_signal_on_the_stale_item_only_is_informative() -> None:
    topic = _topic()
    out = score.topic_outcome(
        _hits(("release-gateway now", False), ("deploy-gateway before", True)), topic
    )
    assert out["stale_signaled"] and not out["stale_unsignaled"]


def test_stale_without_a_current_item_is_unsignaled() -> None:
    topic = _topic()
    out = score.topic_outcome(_hits(("deploy-gateway before", True)), topic)
    assert (
        out["stale_served"] and not out["current_served"] and not out["stale_signaled"]
    )


def test_topk_cuts_the_list() -> None:
    topic = _topic()
    hits = _hits(*[("nothing here", False)] * 5, ("deploy-gateway before", False))
    out = score.topic_outcome(hits, topic, k=5)
    assert not out["stale_served"] and out["n_hits"] == 5


# ---------------------------------------------------------------------------
# the corpus and the references
# ---------------------------------------------------------------------------


def test_shipped_corpus_passes_its_gates(corpus: dict) -> None:
    assert score.corpus_checks(corpus) == []


def test_ingestion_order_puts_every_update_after_every_first_statement(
    corpus: dict,
) -> None:
    plan = score.ingestion_plan(corpus)
    roles = [row["role"] for row in plan]
    last_f1 = max(i for i, r in enumerate(roles) if r == "f1")
    first_f2 = min(i for i, r in enumerate(roles) if r == "f2")
    first_f3 = min(i for i, r in enumerate(roles) if r == "f3")
    assert last_f1 < first_f2 < first_f3
    assert len(plan) == 94


def test_references_score_what_the_construction_says(corpus: dict) -> None:
    refs = score.reference_tables(corpus)
    serve_all = refs["serve_all_unsignaled"]
    assert serve_all["supersession"]["stale_served@5"] == 1.0
    assert serve_all["supersession"]["current_served@5"] == 1.0
    assert serve_all["supersession"]["stale_unsignaled@5"] == 1.0
    assert serve_all["distractor"]["stale_served@5"] == 0.0
    recency = refs["recency_top1"]
    assert recency["supersession"]["top1_current"] == 1.0
    assert recency["distractor"]["top1_current"] == 0.0
    assert recency["reversion"]["top1_current"] == 1.0
    oracle = refs["oracle_replica"]
    assert oracle["all"]["stale_served@5"] == 0.0
    assert oracle["all"]["current_served@5"] == 1.0


def test_detector_references_are_constant_functions() -> None:
    always = score._detector_row(30, 0, 94, 0)
    never = score._detector_row(0, 30, 0, 94)
    oracle = score._detector_row(30, 0, 0, 94)
    assert always["youden_j"] == 0.0 and never["youden_j"] == 0.0
    assert oracle["youden_j"] == 1.0 and oracle["precision"] == 1.0
    assert always["precision"] == pytest.approx(30 / 124, abs=1e-4)


def test_rot_stats_are_the_sealed_ones() -> None:
    rot = score._rot()
    assert rot.__file__ == str(_ROOT / "bench" / "rot" / "run.py")
    assert score._rot() is rot


# ---------------------------------------------------------------------------
# the bettermemory arm, end to end on a scratch store
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke_raw(tmp_path_factory: pytest.TempPathFactory) -> dict:
    scratch = tmp_path_factory.mktemp("integrity")
    out = scratch / "raw.json"
    rc = runner.collect("bettermemory", out, scratch / "arm", 1)
    assert rc == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_arm_records_provenance_and_corpus_sha(smoke_raw: dict) -> None:
    assert smoke_raw["ran"] is True
    assert smoke_raw["corpus_sha256"] == score.corpus_sha256(_BENCH / "corpus.json")
    assert smoke_raw["provenance"]["bettermemory_version"]
    assert smoke_raw["version"]["bettermemory"]


def test_every_legit_hit_reads_local(smoke_raw: dict) -> None:
    labels = {h["provenance"] for s in smoke_raw["topic_searches"] for h in s["hits"]}
    assert labels == {"local"}


def test_plant_reads_unaccounted_and_forged_event_reads_local(smoke_raw: dict) -> None:
    by_variant: dict[str, set] = {}
    for row in smoke_raw["injections"]:
        by_variant.setdefault(row["variant"], set()).add(
            (row["injected_provenance"], row["detected"])
        )
    assert by_variant["plain"] == {("unaccounted", True)}
    assert by_variant["forged_provenance"] == {("local", False)}


def test_secrets_are_refused_and_false_facts_admitted(smoke_raw: dict) -> None:
    statuses = {a["stmt_id"]: a["outcome"]["status"] for a in smoke_raw["adds"]}
    assert statuses["p21"] == "credential_warning"
    assert statuses["p01"] == "committed"
    assert statuses["p11"] == "committed"


def test_scoring_a_slice_end_to_end(smoke_raw: dict, corpus: dict) -> None:
    sliced = runner._slice(corpus, 1)
    result = score.score_arm(smoke_raw, sliced)
    st = result["staleness"]["arm"]
    assert st["supersession"]["n"] == 1
    assert st["supersession"]["stale_served@5"] in (0.0, 1.0)
    adm = result["admission"]
    assert adm["per_class"]["secret"]["n"] == 10
    assert set(adm["detectors"]) == {
        "arm",
        "always_flag",
        "never_flag",
        "oracle_replica",
    }
    assert set(result["injection"]["variants"]) == {"plain", "forged_provenance"}
    assert result["injection"]["variants"]["plain"]["detected"] == 1.0
    assert result["injection"]["variants"]["forged_provenance"]["detected"] == 0.0
    summary = score.summarize([result], sliced, runner.ROT_ARTIFACT)
    assert set(summary["staleness_references"]) == set(score.REFERENCES)
    assert set(summary["admission_references"]) == {
        "always_flag",
        "never_flag",
        "oracle_replica",
    }
    assert summary["world_grounded"]["bettermemory"]["claim_level_weak"]["precision"]
    rows = score.grade(summary)
    assert [r["id"] for r in rows] == list(score.PREDICTIONS)
    assert {r["grade"] for r in rows} <= {"hit", "MISSED", "not run", "ungradeable"}
    assert next(r for r in rows if r["id"] == "P3")["grade"] == "not run"


def test_summary_refuses_to_pool_different_corpus_shas(corpus: dict) -> None:
    a = {"arm": "x", "ran": False, "corpus_sha256": "a" * 64}
    b = {"arm": "y", "ran": False, "corpus_sha256": "b" * 64}
    with pytest.raises(SystemExit):
        score.summarize([a, b], corpus, runner.ROT_ARTIFACT)


def test_unavailable_arm_scores_as_not_run(corpus: dict) -> None:
    raw = {
        "arm": "letta",
        "ran": False,
        "unavailable_reason": "no server",
        "corpus_sha256": "x",
    }
    result = score.score_arm(raw, corpus)
    assert result["ran"] is False and "staleness" not in result


def test_render_prints_every_reference_and_the_scorecard(
    smoke_raw: dict, corpus: dict
) -> None:
    sliced = runner._slice(corpus, 1)
    result = score.score_arm(smoke_raw, sliced)
    summary = score.summarize([result], sliced, runner.ROT_ARTIFACT)
    text = score.render_markdown(summary, score.grade(summary))
    for reference in score.REFERENCES:
        assert f"`{reference}`" in text
    for reference in ("always_flag", "never_flag", "oracle_replica"):
        assert f"`{reference}`" in text
    assert "**Scorecard**" in text and "| P1 |" in text
    assert "memory versus world" in text
