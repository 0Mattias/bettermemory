"""The committed v0 integrity record re-scores to itself.

The integrity scorer took on the v1 format (declared counts, organisations,
split payloads, the test-split guard, LLM usage) as one unit whose
acceptance is that none of it reaches v0. So, at this tree:

- every committed v0 result under bench/integrity/results/ is re-scored
  from its raw file under results/raw/ with `run.py score`;
- every committed summary is re-pooled from the results it names with
  `run.py summary`, and every scorecard re-graded from its summary with
  `run.py scorecard`;
- every committed decision-instrument result under
  bench/decisions/results/ is re-scored offline from the answers its
  per-item records carry, through decisions_integrity.py's own saved-
  answers path (the `chat` instrument, which calls nothing);

and each equals its committed file in every field but the stamps below,
key order included. For results, summaries and scorecards the re-scored
file, with the committed stamps put back, is also compared with the
committed text itself (read with universal newlines, and with the path
fields the command writes in the machine's own form, so the Windows leg
compares what it wrote).

THE STAMPS EXCLUDED, AND WHY.
- A result's `scored_with`: the scorer's version, commit, date and
  machine, and since v1 the corpus it read (`scored_with.corpus`).
- A result's `raw_observations`: the path the raw file was read from;
  three committed results name a scratchpad or another checkout.
- A summary's and a scorecard's `provenance`: the same stamp as
  `scored_with`, the corpus included (`provenance.corpus`).
- A decision result's run stamps `instrument`, `version`, `seconds`,
  `usage` and `run_at`: the offline re-score runs no instrument, and four
  of the five committed files predate the `usage` key; and `provenance`,
  new with --corpus.

FOUR RESULTS AND ONE SUMMARY DID NOT REPRODUCE BEFORE THIS UNIT. The
2026-09-04 results of bettermemory, graphiti, letta and mem0-raw were
scored at bf9e9f4, before the scorer wrote `supersession_writes`;
re-scored at ef496de, the tree this unit started from, each gains
`"supersession_writes": null` and nothing else, and the 2026-09-04
summary gains the same key under those four arms. DRIFT pins exactly
that: the re-scored file less the drift equals the committed text, and
any other difference fails. One raw file has no committed result
(mem0-infer-2026-09-04-no-self-test.json); it is re-scored for the
record, with nothing to compare it with.
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
_BENCH = _ROOT / "bench" / "integrity"
_RESULTS = _BENCH / "results"
_RAW = _RESULTS / "raw"
_DECISIONS = _ROOT / "bench" / "decisions"


def _load(name: str, path: Path) -> ModuleType:
    """The module at `path`, reusing the one another test module loaded
    under the same name from the same file, so a monkeypatch on it reaches
    the code that imports it by name."""
    module = sys.modules.get(name)
    if module is not None and getattr(module, "__file__", None) == str(path):
        return module
    if str(_BENCH) not in sys.path:
        sys.path.insert(0, str(_BENCH))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = _load("score", _BENCH / "score.py")
runner = _load("bench_integrity_run", _BENCH / "run.py")

V0_CORPUS_STAMP_PATH = "bench/integrity/corpus.json"
RESULT_STAMPS = ("scored_with", "raw_observations")
DECISION_STAMPS = ("instrument", "version", "seconds", "usage", "run_at", "provenance")

RESULTS = sorted(
    p
    for p in _RESULTS.glob("integrity-v0-*.json")
    if "-summary-" not in p.name and "-scorecard-" not in p.name
)
SUMMARIES = sorted(_RESULTS.glob("integrity-v0-summary-*.json"))
SCORECARDS = sorted(_RESULTS.glob("integrity-v0-scorecard-*.json"))
# A run's saved answers (``<result>.answers.json``) sit beside the results
# in a working checkout; they are gitignored and are not results.
DECISION_RESULTS = sorted(
    p
    for p in (_DECISIONS / "results").glob("integrity-*.json")
    if not p.name.endswith(".answers.json")
)

# What ef496de's own scorer already adds to these committed files (see the
# module docstring): pinned, not excluded.
DRIFT: dict[str, dict[str, Any]] = {
    name: {"supersession_writes": None}
    for name in (
        "integrity-v0-bettermemory-2026-09-04.json",
        "integrity-v0-graphiti-2026-09-04.json",
        "integrity-v0-letta-2026-09-04.json",
        "integrity-v0-mem0-raw-2026-09-04.json",
    )
}
SUMMARY_DRIFT: dict[str, tuple[str, ...]] = {
    "integrity-v0-summary-2026-09-04.json": (
        "bettermemory",
        "mem0-raw",
        "graphiti",
        "letta",
    )
}


def _raw_for(result: Path) -> Path:
    return _RAW / result.name[len("integrity-v0-") :]


def _text(payload: dict[str, Any]) -> str:
    """The bytes `run.py` writes, as text."""
    return json.dumps(payload, indent=1, sort_keys=False, default=str) + "\n"


def _machine(path: str) -> str:
    """A recorded repo path in the form this machine's commands write it."""
    return str(Path(path))


def _v0_corpus_stamp(sha: str) -> dict[str, Any]:
    return {"path": V0_CORPUS_STAMP_PATH, "sha256": sha, "benchmark": "integrity-v0"}


def test_the_record_is_what_this_file_expects() -> None:
    assert len(RESULTS) == 11 and len(SUMMARIES) == 3 and len(SCORECARDS) == 3
    assert len(DECISION_RESULTS) == 5
    raws = {p.name for p in _RAW.glob("*.json")}
    covered = {_raw_for(r).name for r in RESULTS}
    assert covered <= raws
    assert raws - covered == {"mem0-infer-2026-09-04-no-self-test.json"}


@pytest.mark.parametrize("committed_path", RESULTS, ids=lambda p: p.name)
def test_committed_result_rescores_to_itself(
    committed_path: Path, tmp_path: Path
) -> None:
    out = tmp_path / committed_path.name
    assert runner.cmd_score(_raw_for(committed_path), out) == 0
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    rescored = json.loads(out.read_text(encoding="utf-8"))
    drift = DRIFT.get(committed_path.name, {})
    for key, value in drift.items():
        assert key not in committed and rescored[key] == value
    expected = {k: v for k, v in committed.items() if k not in RESULT_STAMPS}
    got = {
        k: v for k, v in rescored.items() if k not in RESULT_STAMPS and k not in drift
    }
    assert got == expected
    assert list(got) == list(expected)
    restamped = {k: v for k, v in rescored.items() if k not in drift}
    restamped["raw_observations"] = committed["raw_observations"]
    restamped["scored_with"] = committed["scored_with"]
    assert _text(restamped) == committed_path.read_text(encoding="utf-8")
    assert rescored["scored_with"]["corpus"] == _v0_corpus_stamp(
        committed["corpus_sha256"]
    )


def test_the_raw_file_without_a_result_still_scores(tmp_path: Path) -> None:
    raw_path = _RAW / "mem0-infer-2026-09-04-no-self-test.json"
    out = tmp_path / "result.json"
    assert runner.cmd_score(raw_path, out) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["arm"] == "mem0-infer" and result["ran"] is True
    assert "groups" not in result["admission"]


@pytest.mark.parametrize("committed_path", SUMMARIES, ids=lambda p: p.name)
def test_committed_summary_repools_to_itself(
    committed_path: Path, tmp_path: Path
) -> None:
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    out = tmp_path / committed_path.name
    assert runner.cmd_summary([_ROOT / p for p in committed["results"]], out) == 0
    rescored = json.loads(out.read_text(encoding="utf-8"))
    assert rescored["provenance"]["corpus"] == _v0_corpus_stamp(
        committed["corpus_sha256"]
    )
    restamped = dict(rescored)
    restamped["provenance"] = committed["provenance"]
    # the paths the command writes, in this machine's form
    assert restamped["results"] == [_machine(p) for p in committed["results"]]
    restamped["results"] = committed["results"]
    source = committed["world_grounded"]["source"]
    assert restamped["world_grounded"]["source"] == _machine(source)
    restamped["world_grounded"]["source"] = source
    for arm in SUMMARY_DRIFT.get(committed_path.name, ()):
        assert "supersession_writes" not in committed["arms"][arm]
        assert restamped["arms"][arm].pop("supersession_writes") is None
    assert _text(restamped) == committed_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("committed_path", SCORECARDS, ids=lambda p: p.name)
def test_committed_scorecard_regrades_to_itself(
    committed_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    monkeypatch.chdir(_ROOT)  # the scorecard records the summary path as given
    out = tmp_path / committed_path.name
    assert runner.cmd_scorecard(Path(committed["summary"]), out) == 0
    rescored = json.loads(out.read_text(encoding="utf-8"))
    summary = json.loads((_ROOT / committed["summary"]).read_text(encoding="utf-8"))
    assert rescored["provenance"]["corpus"] == _v0_corpus_stamp(
        summary["corpus_sha256"]
    )
    restamped = dict(rescored)
    restamped["provenance"] = committed["provenance"]
    assert restamped["summary"] == _machine(committed["summary"])
    restamped["summary"] = committed["summary"]
    assert _text(restamped) == committed_path.read_text(encoding="utf-8")


def test_the_markdown_captions_on_the_v0_corpus_are_v0s() -> None:
    corpus = json.loads((_BENCH / "corpus.json").read_text(encoding="utf-8"))
    assert score.caption_counts(corpus) == score.caption_counts(None)
    summary = json.loads(SUMMARIES[-1].read_text(encoding="utf-8"))
    rows = score.grade(summary)
    text = score.render_markdown(summary, rows, corpus)
    assert text == score.render_markdown(summary, rows)
    assert "(24 supersession, 8 distractor, 8 reversion topics; k = 5)" in text
    assert "(30 payloads against 94 legitimate statements;" in text
    assert "(10 false facts inserted around the write API;" in text


# ---------------------------------------------------------------------------
# the decision instruments' results, re-scored offline
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def decisions() -> ModuleType:
    """decisions_integrity.py, imported with the process's import path left
    as it was (the module prepends bench paths for its own imports)."""
    saved = list(sys.path)
    try:
        return _load(
            "bench_decisions_decisions_integrity", _DECISIONS / "decisions_integrity.py"
        )
    finally:
        sys.path[:] = saved


def _answers_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The saved-answers file a result's per-item records imply: each
    item's three probabilities and its relation choice and probabilities,
    everything the driver reads from an answer."""
    answers: dict[str, Any] = {}
    for item in items:
        row: dict[str, Any] = {k: {"noul": v} for k, v in item["p"].items()}
        if item["relation"] is not None or item["relation_p"] is not None:
            row["relation"] = {
                "choice": item["relation"],
                "probabilities": item["relation_p"],
            }
        answers[item["stmt_id"]] = row
    return answers


@pytest.mark.parametrize("committed_path", DECISION_RESULTS, ids=lambda p: p.name)
def test_committed_decision_result_rescores_offline_to_itself(
    committed_path: Path, tmp_path: Path, decisions: ModuleType
) -> None:
    committed = json.loads(committed_path.read_text(encoding="utf-8"))
    out = tmp_path / committed_path.name
    out.with_suffix(".answers.json").write_text(
        json.dumps(_answers_from_items(committed["items"])), encoding="utf-8"
    )
    assert decisions.main(["--instrument", "chat", "--out", str(out)]) == 0
    rescored = json.loads(out.read_text(encoding="utf-8"))
    expected = {k: v for k, v in committed.items() if k not in DECISION_STAMPS}
    got = {k: v for k, v in rescored.items() if k not in DECISION_STAMPS}
    assert got == expected
    assert list(got) == list(expected)
    assert rescored["provenance"]["corpus"] == _v0_corpus_stamp(
        committed["corpus_sha256"]
    )


def test_jev_tokens_per_char_is_the_one_measured_on_v0(decisions: ModuleType) -> None:
    """The estimate's rate is the measured one: on the v0 plan, 124 calls
    and 205,500 characters against the 81,818 input tokens Jev's
    responses reported, 0.398 a character."""
    corpus = json.loads((_BENCH / "corpus.json").read_text(encoding="utf-8"))
    items = decisions.build_items(decisions.build_plan(corpus))
    estimate = decisions.instruments.estimate_jev_cost(
        [it["state"] for it in items], [it["questions"] for it in items]
    )
    assert estimate["calls"] == 124 and estimate["chars"] == 205_500
    jev = json.loads(
        (_DECISIONS / "results" / "integrity-jev-1.13.json").read_text(encoding="utf-8")
    )
    measured = jev["usage"]["input_tokens"]
    assert measured == 81_818
    assert round(measured / estimate["chars"], 3) == 0.398
    assert decisions.instruments.JEV_TOKENS_PER_CHAR == 0.4
    assert abs(estimate["tokens_estimated"] - measured) / measured < 0.01
