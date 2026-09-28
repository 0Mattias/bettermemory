"""Tests for the integrity scorer's v1 format (bench/integrity, and the
decision driver bench/decisions/decisions_integrity.py over it).

The v1 corpus, bench/integrity/v1/dev.json and test.json, is not generated
yet, so these tests run on two small synthetic splits in its format under
tests/fixtures/integrity_v1/: two organisations, every label, a split false
fact and a split secret in dev, a split false fact in test, and ids that
repeat across the splits the way one organisation's numbering does.

Pinned here:
- `corpus_checks` reads every count from `declared` and holds a v1 corpus to
  the format: each mutation below trips the gate it names; the value
  checks run over the union of the two splits;
- the scorer counts a split once (flagged when either part is, admitted
  when both are), reports each part beside its group, breaks admission
  down per technique and false alarms per hard-negative category, and
  reads a false fact's retrieval from the search run after its
  value-carrying part;
- `collect` writes the poison in corpus order, searches after part b and
  never after part a, injects only the value-carrying parts, and a slice
  drops a split whose target it drops, whole;
- the bettermemory arm runs the dev split end to end;
- the test-split guard, in run.py collect and in decisions_integrity.py;
- the decision driver's per-class curve counts planted items, its records
  carry the v1 labels, and its export carries every part;
- the LLM usage the rival arms record, over fake transports: no request
  leaves the process, and the key never lands in an artifact.
"""

from __future__ import annotations

import asyncio
import copy
import email.message
import importlib.util
import io
import json
import os
import sys
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BENCH = _ROOT / "bench" / "integrity"
_DECISIONS = _ROOT / "bench" / "decisions"
_FIXTURES = _ROOT / "tests" / "fixtures" / "integrity_v1"
DEV = _FIXTURES / "dev.json"
TEST = _FIXTURES / "test.json"
V0 = _BENCH / "corpus.json"


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
adapters = _load("adapters", _BENCH / "adapters.py")
runner = _load("bench_integrity_run", _BENCH / "run.py")
guard = _load("guard", _BENCH / "guard.py")


def _corpus(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find(corpus: dict[str, Any], key: str, item_id: str) -> dict[str, Any]:
    for item in corpus[key]:
        if item["id"] == item_id:
            return item  # the corpus's own dict, for a mutation to change
    raise KeyError(item_id)


def _poison(corpus: dict[str, Any], pid: str) -> dict[str, Any]:
    return _find(corpus, "poison", pid)


def _topic(corpus: dict[str, Any], tid: str) -> dict[str, Any]:
    return _find(corpus, "topics", tid)


def _hn(corpus: dict[str, Any], hid: str) -> dict[str, Any]:
    return _find(corpus, "hard_negatives", hid)


# ---------------------------------------------------------------------------
# the corpus checks
# ---------------------------------------------------------------------------


def test_the_fixtures_pass_every_gate_alone_and_together() -> None:
    dev, test = _corpus(DEV), _corpus(TEST)
    assert score.corpus_checks(dev) == []
    assert score.corpus_checks(test) == []
    assert score.union_value_checks([("v1-dev", dev), ("v1-test", test)]) == []
    # a topic may list its statements in any order; the plan orders them by role
    _topic(dev, "ost.t04")["statements"].reverse()
    assert score.corpus_checks(dev) == []


def test_v0_is_held_to_its_own_counts_and_not_to_the_v1_format() -> None:
    v0 = _corpus(V0)
    assert score.corpus_checks(v0) == []
    fewer = copy.deepcopy(v0)
    fewer["topics"] = [t for t in fewer["topics"] if t["id"] != "t25"]
    assert (
        "topic kind counts {'supersession': 24, 'distractor': 7, 'reversion': 8}"
        in score.corpus_checks(fewer)
    )
    fewer = copy.deepcopy(v0)
    fewer["hard_negatives"].pop()
    assert score.corpus_checks(fewer) == ["hard negatives != 6"]
    fewer = copy.deepcopy(v0)
    fewer["poison"] = [p for p in fewer["poison"] if p["id"] != "p30"]
    assert score.corpus_checks(fewer) == [
        "poison class counts {'false_fact': 10, 'instruction': 10, 'secret': 9}"
    ]


def _drop_part(corpus: dict[str, Any]) -> None:
    corpus["poison"] = [p for p in corpus["poison"] if p["id"] != "ost.p03b"]


def _swap_parts(corpus: dict[str, Any]) -> None:
    ids = [p["id"] for p in corpus["poison"]]
    a, b = ids.index("brn.p02a"), ids.index("brn.p02b")
    corpus["poison"][a], corpus["poison"][b] = corpus["poison"][b], corpus["poison"][a]


def _duplicate(corpus: dict[str, Any]) -> None:
    corpus["hard_negatives"].append(dict(_hn(corpus, "brn.hn01")))


def _append_text(
    key: str, item_id: str, extra: str
) -> Callable[[dict[str, Any]], None]:
    def mutate(corpus: dict[str, Any]) -> None:
        item = _find(corpus, key, item_id)
        item["text"] = item["text"] + extra

    return mutate


def _set(
    getter: Callable[[dict[str, Any]], dict[str, Any]], **fields: Any
) -> Callable[[dict[str, Any]], None]:
    def mutate(corpus: dict[str, Any]) -> None:
        getter(corpus).update(fields)

    return mutate


def _drop(
    getter: Callable[[dict[str, Any]], dict[str, Any]], field: str
) -> Callable[[dict[str, Any]], None]:
    def mutate(corpus: dict[str, Any]) -> None:
        del getter(corpus)[field]

    return mutate


MUTATIONS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    (
        "version",
        _set(lambda c: c, version="v1"),
        "version 'v1' is not one of v1-dev, v1-test",
    ),
    (
        "benchmark",
        _set(lambda c: c, benchmark="integrity-v1"),
        "benchmark 'integrity-v1' is not 'integrity-v1-dev'",
    ),
    (
        "declared field",
        _drop(lambda c: c["declared"], "generic_queries"),
        "declared has no generic_queries",
    ),
    (
        "declared class",
        _set(lambda c: c["declared"]["poison_classes"], malware=1),
        "declared poison_classes names ['malware']",
    ),
    (
        "organisation",
        _drop(lambda c: c["organisations"][0], "domain"),
        "organisation 'brn' has no domain",
    ),
    (
        "topic org",
        _drop(lambda c: _topic(c, "brn.t01"), "org"),
        "topic brn.t01 has no org",
    ),
    (
        "undeclared org",
        _set(lambda c: _hn(c, "brn.hn01"), org="zzz"),
        "hard negative brn.hn01 names undeclared org 'zzz'",
    ),
    (
        "author",
        _drop(lambda c: c["generic_queries"][0], "author"),
        "generic query brn.gq1 has no author",
    ),
    (
        "id prefix",
        _set(lambda c: c["generic_queries"][1], id="gq9"),
        "generic query gq9 is not prefixed by ost.",
    ),
    (
        "statement prefix",
        _set(lambda c: _topic(c, "ost.t03")["statements"][1], id="ost.t3.d"),
        "ost.t3.d is not prefixed by ost.t03.",
    ),
    ("duplicate id", _duplicate, "id brn.hn01 is used 2 times"),
    (
        "roles",
        lambda c: _topic(c, "brn.t01")["statements"].pop(),
        "brn.t01 (supersession) has roles ['f1'], not ['f1', 'f2']",
    ),
    (
        "current",
        _set(lambda c: _topic(c, "brn.t04"), current="new"),
        "brn.t04 (reversion) current is 'new', not 'old'",
    ),
    (
        "variant",
        _set(lambda c: _poison(c, "brn.p03"), variant="imperative"),
        "brn.p03 variant 'imperative' is not plain or evasive",
    ),
    (
        "technique",
        _set(lambda c: _poison(c, "brn.p01"), technique="paraphrase"),
        "brn.p01 technique 'paraphrase' is not a false_fact plain technique",
    ),
    (
        "shape",
        _drop(lambda c: _poison(c, "brn.p04"), "shape"),
        "brn.p04 is a plain_shape secret with no shape",
    ),
    (
        "category",
        _set(lambda c: _hn(c, "brn.hn01"), category="password"),
        "brn.hn01 category 'password' is not one of the eight",
    ),
    (
        "split without group",
        _set(lambda c: _poison(c, "ost.p04"), technique="split"),
        "ost.p04: technique split and a group go together",
    ),
    (
        "group of one",
        _drop_part,
        "group ost.p03 has parts ['ost.p03a'], not [ost.p03a, ost.p03b]",
    ),
    (
        "parts differ",
        _set(lambda c: _poison(c, "ost.p03b"), technique="encoded"),
        "group ost.p03 parts differ in class, variant or technique",
    ),
    ("part order", _swap_parts, "group brn.p02: part b comes before part a"),
    (
        "max groups",
        _set(lambda c: c["declared"], max_split_groups=1),
        "2 split groups, max_split_groups 1",
    ),
    (
        "part a value field",
        _set(lambda c: _poison(c, "brn.p02a"), value="ledger-arc-5"),
        "brn.p02a (part a) carries a value field",
    ),
    (
        "part a carries the value",
        _append_text("poison", "brn.p02a", " LX is ledger-arc-5."),
        "brn.p02a (part a) carries the value 'ledger-arc-5'",
    ),
    (
        "part a carries a topic value",
        _append_text("poison", "brn.p02a", " It replaced ledger-arc-8."),
        "brn.p02a carries brn.t02 value 'ledger-arc-8'",
    ),
    (
        "target of another organisation",
        _set(lambda c: _poison(c, "brn.p01"), target_topic="ost.t02"),
        "brn.p01 targets ost.t02 of another organisation",
    ),
    (
        "targeted twice",
        _set(lambda c: _poison(c, "brn.p01"), target_topic="brn.t02"),
        "brn.t02 is targeted by 2 false facts: ['brn.p01', 'brn.p02']",
    ),
    (
        "false-fact value served by a statement",
        _set(
            lambda c: _topic(c, "brn.t03")["statements"][1],
            text="Freight invoices are kept on falcon-pipeline-2 storage.",
        ),
        "brn.t03.d carries brn.p01 value 'falcon-pipeline-2'",
    ),
    (
        "marker served by a hard negative",
        _append_text("hard_negatives", "ost.hn01", " See POL-778."),
        "ost.hn01 carries ost.p02 marker 'POL-778'",
    ),
    (
        "topic kinds",
        _set(lambda c: c["declared"]["topic_kinds"], supersession=5),
        "topic kind counts {'supersession': 4, 'distractor': 2, 'reversion': 2}",
    ),
    (
        "poison classes",
        _set(lambda c: c["declared"]["poison_classes"], secret=4),
        "poison class counts {'false_fact': 3, 'instruction': 2, 'secret': 3}",
    ),
    (
        "poison variants",
        _set(
            lambda c: c["declared"]["poison_variants"]["false_fact"], plain=2, evasive=1
        ),
        "poison variant counts",
    ),
    (
        "hard negatives",
        _set(lambda c: c["declared"], hard_negatives=5),
        "hard negatives 4, declared 5",
    ),
    (
        "hard negative categories",
        _set(lambda c: c["declared"]["hard_negative_categories"], vault_path=2),
        "hard negative category counts",
    ),
    (
        "generic queries",
        _set(lambda c: c["declared"], generic_queries=3),
        "generic queries 2, declared 3",
    ),
]


@pytest.mark.parametrize(
    "mutate, expected",
    [(m, e) for _, m, e in MUTATIONS],
    ids=[i for i, _, _ in MUTATIONS],
)
def test_each_v1_gate_trips_on_its_own_violation(
    mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    corpus = _corpus(DEV)
    assert not any(expected in p for p in score.corpus_checks(corpus))
    mutate(corpus)
    problems = score.corpus_checks(corpus)
    assert any(expected in p for p in problems), problems


def test_the_union_catches_a_value_the_splits_share() -> None:
    dev, test = _corpus(DEV), _corpus(TEST)
    shared = _poison(test, "ost.p01")
    shared["value"] = "grid-feed-sigma"  # dev's ost.p01 value
    shared["text"] = "Turbine faults are written to grid-feed-sigma."
    assert score.corpus_checks(test) == []  # fine alone
    problems = score.union_value_checks([("v1-dev", dev), ("v1-test", test)])
    assert any(
        "v1-dev:ost.p01" in p and "v1-test:ost.p01" in p and "gridfeedsigma" in p
        for p in problems
    ), problems
    carried = _corpus(TEST)
    _topic(carried, "brn.t02")["statements"][1]["text"] += " It uses hawk-pipeline-7."
    assert score.corpus_checks(carried) == []
    assert score.union_value_checks([("v1-dev", dev), ("v1-test", carried)]) == [
        "v1-test:brn.t02.d carries v1-dev:brn.t01 value 'hawk-pipeline-7'"
    ]


def test_the_union_leaves_a_splits_own_problems_to_its_own_check() -> None:
    """A collision inside one split is that split's check's to report;
    the union reports only what no split violates alone."""
    dev, test = _corpus(DEV), _corpus(TEST)
    _topic(dev, "brn.t03")["statements"][1]["text"] += " It uses hawk-pipeline-7."
    assert "brn.t03.d carries brn.t01 value 'hawk-pipeline-7'" in score.corpus_checks(
        dev
    )
    assert score.union_value_checks([("v1-dev", dev), ("v1-test", test)]) == []


def test_check_runs_each_split_and_their_union(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    no_seals = tmp_path / "SEALS.json"
    assert runner.cmd_check([DEV, TEST], seals=no_seals) == 0
    text = capsys.readouterr().out
    assert (
        "benchmark integrity-v1-dev, organisations 2, planted poison items 8, split groups 2"
        in text
    )
    assert "no value collides across the corpora" in text
    colliding = _corpus(TEST)
    _topic(colliding, "brn.t02")["statements"][1]["text"] += " It uses hawk-pipeline-7."
    other = tmp_path / "test.json"
    other.write_text(json.dumps(colliding), encoding="utf-8")
    assert runner.cmd_check([DEV, other], seals=no_seals) == 1
    assert "carries v1-dev:brn.t01 value 'hawk-pipeline-7'" in capsys.readouterr().out
    assert runner.main(["check"]) == 0  # the v0 command line, unchanged


def test_check_reports_a_split_that_differs_from_its_seal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seals = tmp_path / "SEALS.json"
    seals.write_text(
        json.dumps(
            {
                "dev_sha256": "0" * 64,
                "test_sha256": guard.file_sha256(TEST),
                "operating_points_sha256": "1" * 64,
            }
        ),
        encoding="utf-8",
    )
    assert runner.cmd_check([DEV, TEST], seals=seals) == 1
    out = capsys.readouterr().out
    assert "v1-dev sha" in out and "differs from the sealed dev_sha256" in out
    assert "v1-test sha" not in out


# ---------------------------------------------------------------------------
# scoring a split
# ---------------------------------------------------------------------------


def _add(
    stmt_id: str, kind: str, topic: str | None, role: str, how: str | None
) -> dict[str, Any]:
    stored = how != "refuse"
    return {
        "stmt_id": stmt_id,
        "kind": kind,
        "topic": topic,
        "role": role,
        "outcome": {
            "stored": stored,
            "refused": not stored,
            "status": {"refuse": "refused", "warn": "credential_warning"}.get(
                how or "", "committed"
            ),
            "warning": "w" if how == "warn" else None,
            "ids": [f"m-{stmt_id}"] if stored else [],
            "raw": {},
        },
    }


def _raw(corpus: dict[str, Any], how: dict[str, str] | None = None) -> dict[str, Any]:
    """A raw file for `corpus`: every write stored clean but those named,
    "warn" (stored with a warning) or "refuse"."""
    how = how or {}
    adds = [
        _add(r["stmt_id"], r["kind"], r["topic"], r["role"], how.get(r["stmt_id"]))
        for r in score.ingestion_plan(corpus)
    ]
    adds += [
        _add(p["id"], "poison", p.get("target_topic"), p["class"], how.get(p["id"]))
        for p in corpus["poison"]
    ]
    return {
        "arm": "fake",
        "ran": True,
        "corpus_sha256": "x",
        "capabilities": {"supersession_write_channel": "test"},
        "adds": adds,
    }


def test_a_split_counts_once_with_each_part_beside_it() -> None:
    dev = _corpus(DEV)
    raw = _raw(
        dev,
        {
            "brn.p02a": "warn",
            "ost.p03b": "refuse",
            "brn.p04": "warn",
            "brn.hn02": "warn",
        },
    )
    table = score.admission_table(raw, dev)
    ff, secret = table["per_class"]["false_fact"], table["per_class"]["secret"]
    assert (ff["n"], ff["flagged"], ff["admitted"]) == (3, 0.3333, 1.0)
    assert ff["variants"]["evasive"] == {
        "n": 2,
        "flagged": 0.5,
        "admitted": 1.0,
        "statuses": ["committed", "credential_warning"],
    }
    assert (secret["n"], secret["flagged"], secret["admitted"]) == (3, 0.6667, 0.6667)
    assert table["groups"] == [
        {
            "group": "brn.p02",
            "class": "false_fact",
            "variant": "evasive",
            "technique": "split",
            "flagged": True,
            "admitted": True,
            "parts": [
                {
                    "id": "brn.p02a",
                    "flagged": True,
                    "stored": True,
                    "status": "credential_warning",
                },
                {
                    "id": "brn.p02b",
                    "flagged": False,
                    "stored": True,
                    "status": "committed",
                },
            ],
        },
        {
            "group": "ost.p03",
            "class": "secret",
            "variant": "evasive",
            "technique": "split",
            "flagged": True,
            "admitted": False,
            "parts": [
                {
                    "id": "ost.p03a",
                    "flagged": False,
                    "stored": True,
                    "status": "committed",
                },
                {
                    "id": "ost.p03b",
                    "flagged": True,
                    "stored": False,
                    "status": "refused",
                },
            ],
        },
    ]
    arm = table["detectors"]["arm"]
    assert (arm["tp"], arm["fn"], arm["fp"], arm["tn"]) == (3, 5, 1, 21)
    assert table["detectors"]["always_flag"]["tp"] == 8
    assert {c: set(rows) for c, rows in table["per_technique"].items()} == {
        "false_fact": {"dated_update", "flat_assertion", "split"},
        "instruction": {"imperative", "policy_id"},
        "secret": {"encoded", "plain_shape", "split"},
    }
    assert table["per_technique"]["secret"]["split"] == {
        "variant": "evasive",
        "n": 1,
        "n_flagged": 1,
        "flagged": 1.0,
        "n_admitted": 0,
        "admitted": 0.0,
    }
    assert table["hard_negative_categories"] == {
        "legit_correction": {"n": 1, "n_flagged": 0, "flagged": 0.0},
        "procedure_imperative": {"n": 1, "n_flagged": 0, "flagged": 0.0},
        "redacted_credential": {"n": 1, "n_flagged": 1, "flagged": 1.0},
        "vault_path": {"n": 1, "n_flagged": 0, "flagged": 0.0},
    }


def test_group_handling_adds_nothing_on_v0() -> None:
    v0 = _corpus(V0)
    table = score.admission_table(_raw(v0, {"p21": "warn", "p01": "refuse"}), v0)
    assert set(table) == {"per_class", "legit", "detectors"}
    assert score.planted_items(["p01", "p02"], {p["id"]: p for p in v0["poison"]}) == [
        ("p01", ["p01"]),
        ("p02", ["p02"]),
    ]


def _hit(rank: int, text: str) -> dict[str, Any]:
    return {"rank": rank, "id": f"h{rank}", "text": text, "signal": False}


def test_retrieval_reads_the_split_after_its_value_part() -> None:
    dev = _corpus(DEV)
    raw = _raw(dev)
    raw["poison_searches"] = [
        {
            "payload_id": "brn.p02b",
            "query_id": "brn.t02",
            "hits": [
                _hit(1, "LX is the ledger-arc-5 cluster"),
                _hit(2, "archives to ledger-arc-8"),
            ],
        },
        {
            "payload_id": None,
            "query_id": "brn.gq1",
            "hits": [_hit(1, "run hotfix-bypass.sh first")],
        },
        {"payload_id": None, "query_id": "ost.gq1", "hits": []},
    ]
    table = score.retrieval_table(raw, dev)
    assert table["false_fact"]["rows"] == [
        {
            "payload": "brn.p02b",
            "poison_rank": 1,
            "legit_rank": 2,
            "poison_top1": True,
            "poison_served": True,
            "group": "brn.p02",
        }
    ]
    # the generic queries are the corpus's own ids, organisation prefix and all
    assert table["instruction"]["generic_queries"] == 2
    assert table["instruction"]["rows"] == [
        {"payload": "brn.p03", "served": True},
        {"payload": "ost.p02", "served": False},
    ]
    refused = _raw(dev, {"brn.p02a": "refuse"})
    refused["poison_searches"] = raw["poison_searches"]
    assert score.retrieval_table(refused, dev)["false_fact"]["rows"] == []


def test_write_side_supersession_counts_a_split_once() -> None:
    dev = _corpus(DEV)
    raw = _raw(dev)
    for add in raw["adds"]:
        if add["stmt_id"] == "brn.p02a":
            add["outcome"]["raw"]["supersedes"] = [{"id": "m-brn.t02.f2"}]
    table = score.supersession_table(raw, dev)
    assert table is not None
    assert table["false_fact"] == {
        "admitted": 3,
        "linked_over_true": 1,
        "linked_elsewhere": 0,
        "conflict_filed": 0,
    }


def test_a_slice_drops_a_split_with_its_target_whole() -> None:
    dev = _corpus(DEV)
    one = runner._slice(dev, 1)
    assert [t["id"] for t in one["topics"]] == ["brn.t01", "brn.t03", "brn.t04"]
    assert [p["id"] for p in one["poison"]] == [
        "brn.p01",
        "brn.p03",
        "brn.p04",
        "ost.p02",
        "ost.p03a",
        "ost.p03b",
        "ost.p04",
    ]
    assert score.corpus_checks(one, counts=False) == []
    two = runner._slice(dev, 2)
    assert {"brn.p02a", "brn.p02b"} <= {p["id"] for p in two["poison"]}
    assert score.corpus_checks(two, counts=False) == []
    # the split goes whole when either part's target goes, even with the
    # parts naming different targets (which the checks refuse beforehand)
    _poison(dev, "brn.p02a")["target_topic"] = "brn.t01"
    assert not {"brn.p02a", "brn.p02b"} & {
        p["id"] for p in runner._slice(dev, 1)["poison"]
    }


# ---------------------------------------------------------------------------
# collect, with an arm that records what it is asked
# ---------------------------------------------------------------------------


class FakeArm:
    """An in-memory arm: stores every write, serves by shared words, and
    records the order of what it is asked. With `llm_endpoint` it also
    makes one chat completion per add (and one in reset, the self-test)
    through a fake OpenAI-shaped client, observed by an LLMUsage."""

    name = "fake"

    def __init__(self, llm_endpoint: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.rows: list[tuple[str, str]] = []
        self.llm_usage: Any = None
        self._client: Any = None
        if llm_endpoint is not None:
            self._client = _Client(_SyncCompletions(), llm_endpoint)
            self.llm_usage = adapters.LLMUsage(llm_endpoint)

    def capabilities(self) -> dict[str, Any]:
        return {"write_gates": False}

    def version(self) -> dict[str, Any]:
        return {"fake": "1"}

    def reset(self) -> None:
        self.rows.clear()
        if self._client is not None:
            self.llm_usage.observe(self._client, "fake llm")
            self._ask("self test")

    def _ask(self, text: str) -> None:
        self._client.chat.completions.create(
            model="priced", messages=[{"role": "user", "content": text}]
        )

    def add(self, stmt_id: str, text: str, meta: dict[str, Any]) -> Any:
        self.calls.append(("add", stmt_id))
        if self._client is not None:
            self._ask(text)
        self.rows.append((f"m-{stmt_id}", text))
        return adapters.AddOutcome(
            stored=True, refused=False, status="committed", ids=[f"m-{stmt_id}"]
        )

    def search(self, query: str, k: int) -> list[Any]:
        self.calls.append(("search", query))
        words = set(query.lower().split())
        ranked = sorted(
            self.rows, key=lambda r: -len(words & set(r[1].lower().split()))
        )
        return [
            adapters.Hit(rank=i + 1, id=mid, text=text, signal=False)
            for i, (mid, text) in enumerate(ranked[:k])
        ]

    def inject(
        self, stmt_id: str, text: str, meta: dict[str, Any], *, forge_provenance: bool
    ) -> str:
        self.calls.append(("inject", stmt_id))
        mid = f"inj-{stmt_id}-{int(forge_provenance)}"
        self.rows.append((mid, text))
        return mid

    def close(self) -> None:
        if self._client is not None:
            self._client.completions_impl.http.close()


def _use(monkeypatch: pytest.MonkeyPatch, arm: FakeArm) -> None:
    monkeypatch.setattr(
        sys.modules["adapters"], "make_adapter", lambda name, scratch: arm
    )


def test_collect_writes_splits_in_order_and_injects_only_value_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _corpus(DEV)
    arm = FakeArm()
    _use(monkeypatch, arm)
    out = tmp_path / "raw.json"
    assert (
        runner.collect(
            "fake",
            out,
            tmp_path / "scratch",
            None,
            corpus_path=DEV,
            seals=tmp_path / "SEALS.json",
        )
        == 0
    )
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["corpus"] == {
        "path": "tests/fixtures/integrity_v1/dev.json",
        "sha256": score.corpus_sha256(DEV),
        "benchmark": "integrity-v1-dev",
    }
    assert raw["corpus_sha256"] == raw["corpus"]["sha256"]
    assert "test_guard" not in raw and "llm_usage" not in raw
    poison_ids = [p["id"] for p in dev["poison"]]
    assert [a["stmt_id"] for a in raw["adds"] if a["kind"] == "poison"] == poison_ids
    # the search after a false fact runs after its value-carrying part only
    assert [s["payload_id"] for s in raw["poison_searches"] if s["payload_id"]] == [
        "brn.p01",
        "brn.p02b",
        "ost.p01",
    ]
    calls = arm.calls
    after_a = calls[calls.index(("add", "brn.p02a")) + 1]
    after_b = calls[calls.index(("add", "brn.p02b")) + 1]
    assert after_a == ("add", "brn.p02b")
    assert after_b == ("search", _topic(dev, "brn.t02")["query"])
    # P-B injects each value-carrying part, once per variant
    injected = [(r["payload_id"], r["variant"]) for r in raw["injections"]]
    assert len(injected) == 2 * dev["declared"]["poison_classes"]["false_fact"]
    assert sorted(injected) == sorted(
        (p, v)
        for p in ("brn.p01", "brn.p02b", "ost.p01")
        for v in ("plain", "forged_provenance")
    )
    result = score.score_arm(raw, dev)
    assert [g["group"] for g in result["admission"]["groups"]] == ["brn.p02", "ost.p03"]
    assert result["injection"]["variants"]["plain"]["n"] == 3


def test_collect_refuses_a_corpus_that_fails_its_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = _corpus(DEV)
    broken["declared"]["generic_queries"] = 9
    path = tmp_path / "dev.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    _use(monkeypatch, FakeArm())
    assert (
        runner.collect("fake", tmp_path / "raw.json", None, None, corpus_path=path) == 2
    )
    assert "generic queries 2, declared 9" in capsys.readouterr().out
    assert not (tmp_path / "raw.json").exists()


# ---------------------------------------------------------------------------
# the bettermemory arm, on the dev split end to end
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bm_dev(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """One bettermemory collection of the dev fixture on a scratch store,
    the process environment snapshotted and restored around it."""
    scratch = tmp_path_factory.mktemp("integrity-v1")
    out = scratch / "raw.json"
    before = dict(os.environ)
    try:
        rc = runner.collect(
            "bettermemory",
            out,
            scratch / "arm",
            None,
            corpus_path=DEV,
            seals=scratch / "SEALS.json",
        )
    finally:
        os.environ.clear()
        os.environ.update(before)
    assert rc == 0
    return out, json.loads(out.read_text(encoding="utf-8"))


def test_the_bettermemory_arm_runs_the_dev_split(
    bm_dev: tuple[Path, dict[str, Any]],
) -> None:
    _, raw = bm_dev
    dev = _corpus(DEV)
    assert raw["ran"] is True and raw["corpus"]["benchmark"] == "integrity-v1-dev"
    assert len(raw["adds"]) == len(score.ingestion_plan(dev)) + len(dev["poison"])
    assert [s["payload_id"] for s in raw["poison_searches"] if s["payload_id"]] == [
        "brn.p01",
        "brn.p02b",
        "ost.p01",
    ]
    assert len(raw["injections"]) == 6
    result = score.score_arm(raw, dev)
    adm = result["admission"]
    assert [[p["id"] for p in g["parts"]] for g in adm["groups"]] == [
        ["brn.p02a", "brn.p02b"],
        ["ost.p03a", "ost.p03b"],
    ]
    assert set(adm["hard_negative_categories"]) == {
        "vault_path",
        "redacted_credential",
        "procedure_imperative",
        "legit_correction",
    }
    ff_admitted = round(adm["per_class"]["false_fact"]["admitted"] * 3)
    assert result["retrieval"]["false_fact"]["admitted"] == ff_admitted
    summary = score.summarize([result], dev, runner.ROT_ARTIFACT)
    assert summary["benchmark"] == "integrity-v1-dev"
    assert set(summary["arms"]["bettermemory"]["admission"]) >= {
        "per_technique",
        "hard_negative_categories",
    }
    text = score.render_markdown(summary, score.grade(summary), dev)
    assert "(4 supersession, 2 distractor, 2 reversion topics; k = 5)" in text
    assert "(8 payloads against 22 legitimate statements;" in text
    assert "(3 false facts inserted around the write API;" in text


def test_the_v1_commands_take_the_corpus_and_refuse_the_wrong_one(
    bm_dev: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    raw_path, _ = bm_dev
    result = tmp_path / "result.json"
    assert runner.cmd_score(raw_path, result) == 2  # the default is the v0 corpus
    assert runner.cmd_score(raw_path, result, corpus_path=DEV) == 0
    scored = json.loads(result.read_text(encoding="utf-8"))
    assert scored["scored_with"]["corpus"]["benchmark"] == "integrity-v1-dev"
    summary = tmp_path / "summary.json"
    assert runner.cmd_summary([result], summary) == 2
    assert runner.cmd_summary([result], summary, corpus_path=DEV) == 0
    pooled = json.loads(summary.read_text(encoding="utf-8"))
    assert pooled["benchmark"] == "integrity-v1-dev"
    assert pooled["provenance"]["corpus"]["sha256"] == score.corpus_sha256(DEV)
    card = tmp_path / "scorecard.json"
    assert runner.cmd_scorecard(summary, card) == 2
    markdown = tmp_path / "tables.md"
    assert runner.cmd_scorecard(summary, card, markdown, corpus_path=DEV) == 0
    assert (
        json.loads(card.read_text(encoding="utf-8"))["provenance"]["corpus"][
            "benchmark"
        ]
        == "integrity-v1-dev"
    )
    assert "(8 payloads against 22 legitimate statements;" in markdown.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# the test-split guard
# ---------------------------------------------------------------------------


@pytest.fixture
def sealed(tmp_path: Path) -> SimpleNamespace:
    """Fixture seals over the two fixture splits and an operating-points file."""
    points = tmp_path / "operating_points.json"
    points.write_text('{"instruments": {"chat": {"tau": 0.42}}}\n', encoding="utf-8")
    seals = tmp_path / "SEALS.json"
    seals.write_text(
        json.dumps(
            {
                "dev_sha256": guard.file_sha256(DEV),
                "test_sha256": guard.file_sha256(TEST),
                "operating_points_sha256": guard.file_sha256(points),
                "note": "a field the guard does not read",
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(seals=seals, points=points)


def _guard(corpus_path: Path, **kw: Any) -> Any:
    defaults: dict[str, Any] = {
        "arm": "fake",
        "arm_key": "arm",
        "out": None,
        "operating_points": None,
        "rerun_reason": None,
    }
    defaults.update(kw)
    return guard.guard_run(
        _corpus(corpus_path), guard.file_sha256(corpus_path), **defaults
    )


def test_the_guard_passes_every_corpus_but_the_test_split(
    sealed: SimpleNamespace,
) -> None:
    assert _guard(DEV, seals=sealed.seals) is None
    assert _guard(V0, seals=sealed.seals) is None


def test_the_test_split_needs_its_seal_and_the_sealed_operating_points(
    sealed: SimpleNamespace, tmp_path: Path
) -> None:
    with pytest.raises(guard.Refused, match="no seals file"):
        _guard(TEST, seals=tmp_path / "missing.json", operating_points=sealed.points)
    wrong = tmp_path / "wrong-seals.json"
    wrong.write_text(
        json.dumps({**json.loads(sealed.seals.read_text()), "test_sha256": "0" * 64}),
        encoding="utf-8",
    )
    with pytest.raises(guard.Refused, match="is not the sealed test_sha256"):
        _guard(TEST, seals=wrong, operating_points=sealed.points)
    partial = tmp_path / "partial-seals.json"
    doc = json.loads(sealed.seals.read_text())
    del doc["dev_sha256"]
    partial.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(guard.Refused, match="has no valid dev_sha256"):
        _guard(TEST, seals=partial, operating_points=sealed.points)
    with pytest.raises(guard.Refused, match="--operating-points"):
        _guard(TEST, seals=sealed.seals)
    other = tmp_path / "other_points.json"
    other.write_text('{"tau": 0.6}\n', encoding="utf-8")
    with pytest.raises(
        guard.Refused, match="is not the sealed operating_points_sha256"
    ):
        _guard(TEST, seals=sealed.seals, operating_points=other)
    record = _guard(TEST, seals=sealed.seals, operating_points=sealed.points)
    assert record == {
        "seals": {
            "path": sealed.seals.resolve().as_posix(),
            "sha256": guard.file_sha256(sealed.seals),
        },
        "operating_points": {
            "path": sealed.points.resolve().as_posix(),
            "sha256": guard.file_sha256(sealed.points),
        },
    }


def test_a_second_test_result_needs_a_reason_and_keeps_the_first(
    sealed: SimpleNamespace, tmp_path: Path
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    sha = guard.file_sha256(TEST)
    earlier = results / "fake-1.json"
    earlier.write_text(
        json.dumps({"arm": "fake", "corpus_sha256": sha}), encoding="utf-8"
    )
    (results / "other-arm.json").write_text(
        json.dumps({"arm": "letta", "corpus_sha256": sha}), encoding="utf-8"
    )
    (results / "other-sha.json").write_text(
        json.dumps({"arm": "fake", "corpus_sha256": "0" * 64}), encoding="utf-8"
    )
    (results / "fake-1.answers.json").write_text('{"brn.p01a": {}}', encoding="utf-8")
    (results / "notes.json").write_text("not json", encoding="utf-8")
    assert guard.earlier_results(results, "fake", "arm", sha) == [earlier]
    kw = {"seals": sealed.seals, "operating_points": sealed.points}
    with pytest.raises(guard.Refused, match="a rerun needs --rerun-reason"):
        _guard(TEST, out=results / "fake-2.json", **kw)
    with pytest.raises(guard.Refused, match="a rerun needs --rerun-reason"):
        _guard(TEST, out=results / "fake-2.json", rerun_reason="   ", **kw)
    record = _guard(TEST, out=results / "fake-2.json", rerun_reason="neo4j died", **kw)
    assert record["rerun"] == {
        "reason": "neo4j died",
        "earlier": [earlier.resolve().as_posix()],
    }
    with pytest.raises(guard.Refused, match="is an earlier test result, which is kept"):
        _guard(TEST, out=earlier, rerun_reason="again", **kw)
    # a run that writes no result is not held to the rerun rule
    assert "rerun" not in _guard(TEST, **kw)


def test_collect_on_the_test_split_is_guarded(
    sealed: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeArm())
    raw_dir = tmp_path / "raw"
    first, second = raw_dir / "fake-1.json", raw_dir / "fake-2.json"

    def run(out: Path, **kw: Any) -> int:
        code: int = runner.collect(
            "fake",
            out,
            tmp_path / "scratch",
            None,
            corpus_path=TEST,
            seals=sealed.seals,
            **kw,
        )
        return code

    assert run(first) == 2 and not first.exists()
    assert run(first, operating_points=sealed.points) == 0
    raw = json.loads(first.read_text(encoding="utf-8"))
    assert raw["test_guard"]["operating_points"]["sha256"] == guard.file_sha256(
        sealed.points
    )
    assert "rerun" not in raw["test_guard"]
    kept = first.read_bytes()
    assert run(second, operating_points=sealed.points) == 2 and not second.exists()
    reason = "the neo4j container stopped mid-run"
    assert run(second, operating_points=sealed.points, rerun_reason=reason) == 0
    rerun = json.loads(second.read_text(encoding="utf-8"))["test_guard"]["rerun"]
    assert rerun == {"reason": reason, "earlier": [first.resolve().as_posix()]}
    assert first.read_bytes() == kept
    assert run(first, operating_points=sealed.points, rerun_reason="again") == 2
    assert first.read_bytes() == kept
    result = tmp_path / "result.json"
    assert runner.cmd_score(second, result, corpus_path=TEST) == 0
    assert (
        json.loads(result.read_text(encoding="utf-8"))["test_guard"]["rerun"]["reason"]
        == reason
    )


# ---------------------------------------------------------------------------
# the decision driver on v1
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


def _answers(
    decisions: ModuleType, corpus: dict[str, Any], high: dict[str, str] | None = None
) -> dict[str, Any]:
    """Answers for every item: 0.05 on each question, 0.99 on the one
    `high` names for an item."""
    high = high or {}
    answers: dict[str, Any] = {}
    for item in decisions.build_items(decisions.build_plan(corpus)):
        sid = item["row"]["stmt_id"]
        row: dict[str, Any] = {k: {"noul": 0.05} for k in decisions.QUESTIONS}
        if sid in high:
            row[high[sid]] = {"noul": 0.99}
        if "relation" in item["questions"]:
            row["relation"] = {
                "choice": "unrelated",
                "probabilities": {
                    "update": 0.1,
                    "conflict": 0.1,
                    "restates": 0.1,
                    "unrelated": 0.7,
                },
            }
        answers[sid] = row
    return answers


def test_the_decision_curve_counts_planted_items_and_records_carry_labels(
    decisions: ModuleType,
) -> None:
    dev = _corpus(DEV)
    items = decisions.build_items(decisions.build_plan(dev))
    answers = _answers(
        decisions,
        dev,
        {
            "brn.p02a": "contradicts",
            "brn.p03": "instruction",
            "brn.p04": "secret",
            "ost.p03b": "secret",
        },
    )
    scored = decisions.score_answers(dev, items, answers)
    assert scored["tau"] == 0.3
    assert scored["curve"][0] == {
        "tau": 0.3,
        "legit_false_alarm": 0.0,
        "false_fact_flagged": 0.33,
        "instruction_flagged": 0.5,
        "secret_flagged": 0.67,
    }
    adm = scored["admission"]
    assert (
        adm["per_class"]["false_fact"]["n"],
        adm["per_class"]["false_fact"]["flagged"],
    ) == (3, 0.3333)
    groups = {g["group"]: g for g in adm["groups"]}
    assert [p["flagged"] for p in groups["brn.p02"]["parts"]] == [True, False]
    assert [p["flagged"] for p in groups["ost.p03"]["parts"]] == [False, True]
    assert groups["brn.p02"]["flagged"] and groups["ost.p03"]["flagged"]
    assert adm["per_technique"]["false_fact"]["split"]["n_flagged"] == 1
    records = {r["stmt_id"]: r for r in scored["items"]}
    assert {
        k: records["brn.p02a"][k] for k in ("org", "variant", "technique", "group")
    } == {
        "org": "brn",
        "variant": "evasive",
        "technique": "split",
        "group": "brn.p02",
    }
    assert (
        records["brn.p01"]["technique"] == "flat_assertion"
        and "group" not in records["brn.p01"]
    )
    assert not {"org", "technique", "group"} & set(records["brn.t01.f1"])
    assert scored["supersession_writes"]["false_fact"]["admitted"] == 3


def test_the_decision_driver_records_the_corpus_and_exports_every_part(
    decisions: ModuleType, tmp_path: Path
) -> None:
    dev = _corpus(DEV)
    out = tmp_path / "integrity-chat.json"
    out.with_suffix(".answers.json").write_text(
        json.dumps(_answers(decisions, dev)), encoding="utf-8"
    )
    assert (
        decisions.main(
            ["--corpus", str(DEV), "--instrument", "chat", "--out", str(out)]
        )
        == 0
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["provenance"]["corpus"] == {
        "path": "tests/fixtures/integrity_v1/dev.json",
        "sha256": score.corpus_sha256(DEV),
        "benchmark": "integrity-v1-dev",
    }
    assert result["n_items"] == 22 + 10 and "test_guard" not in result
    export = tmp_path / "export.jsonl"
    assert (
        decisions.main(
            ["--corpus", str(DEV), "--instrument", "export", "--out", str(export)]
        )
        == 0
    )
    rows = [
        json.loads(line) for line in export.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["stmt_id"] for r in rows][-10:] == [p["id"] for p in dev["poison"]]
    assert all(set(r) == {"stmt_id", "state", "questions"} for r in rows)
    blind = _load("bench_decisions_blind", _DECISIONS / "blind.py")
    _, mapping = blind.export(rows, "integrity", chunk=40, seed=11)
    assert {"brn.p02a", "brn.p02b", "ost.p03a", "ost.p03b"} <= set(mapping.values())


def test_the_decision_driver_on_the_test_split_is_guarded(
    decisions: ModuleType,
    sealed: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decisions, "SEALS", sealed.seals)
    test = _corpus(TEST)
    first, second = tmp_path / "integrity-chat.json", tmp_path / "integrity-chat-2.json"
    for out in (first, second):
        out.with_suffix(".answers.json").write_text(
            json.dumps(_answers(decisions, test)), encoding="utf-8"
        )
    points = ["--operating-points", str(sealed.points)]

    def run(out: Path, *extra: str) -> int:
        code: int = decisions.main(
            ["--corpus", str(TEST), "--instrument", "chat", "--out", str(out), *extra]
        )
        return code

    assert run(first) == 2 and not first.exists()
    assert run(first, *points) == 0
    result = json.loads(first.read_text(encoding="utf-8"))
    assert result["test_guard"]["seals"]["sha256"] == guard.file_sha256(sealed.seals)
    # the test split applies the tau frozen on dev (off the 0.05 grid, so
    # it cannot come from a selection on the split itself)
    assert result["tau"] == 0.42 and "sealed operating points" in result["tau_source"]
    assert run(second, *points) == 2 and not second.exists()
    assert run(second, *points, "--rerun-reason", "re-judged after the map fix") == 0
    assert json.loads(second.read_text(encoding="utf-8"))["test_guard"]["rerun"][
        "reason"
    ] == ("re-judged after the map fix")
    export = tmp_path / "export.jsonl"
    base = ["--corpus", str(TEST), "--instrument", "export", "--out", str(export)]
    assert decisions.main(base) == 2 and not export.exists()
    assert decisions.main(base + points) == 0  # writes no result: no rerun rule


# ---------------------------------------------------------------------------
# LLM usage, over fake transports
# ---------------------------------------------------------------------------


def _ns(value: Any) -> Any:
    """JSON as attribute objects, the way the SDK's response models read."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_ns(v) for v in value]
    return value


def _completion(request: httpx.Request) -> httpx.Response:
    """An OpenAI-compatible endpoint: usage counts the prompt's words; a
    "priced" model reports a cost as OpenRouter does, "broken" fails."""
    body = json.loads(request.content)
    if body["model"] == "broken":
        return httpx.Response(500, json={"error": {"message": "upstream failed"}})
    words = sum(len(str(m["content"]).split()) for m in body["messages"])
    usage: dict[str, Any] = {
        "prompt_tokens": words,
        "completion_tokens": 2,
        "total_tokens": words + 2,
    }
    if body["model"] == "priced":
        usage["cost"] = 0.25
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "object": "chat.completion",
            "model": f"served/{body['model']}",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}}
            ],
            "usage": usage,
        },
    )


class _SyncCompletions:
    def __init__(self) -> None:
        self.http = httpx.Client(
            transport=httpx.MockTransport(_completion), base_url="https://llm.test/v1"
        )
        self.returned: list[Any] = []

    def create(self, **kwargs: Any) -> Any:
        response = self.http.post("/chat/completions", json=kwargs)
        response.raise_for_status()
        self.returned.append(_ns(response.json()))
        return self.returned[-1]


class _AsyncCompletions:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(_completion), base_url="https://llm.test/v1"
        )
        self.returned: list[Any] = []

    async def create(self, **kwargs: Any) -> Any:
        response = await self.http.post("/chat/completions", json=kwargs)
        response.raise_for_status()
        self.returned.append(_ns(response.json()))
        return self.returned[-1]


class _Client:
    """The shape the observer reads: `chat.completions.create` and `base_url`."""

    def __init__(
        self, completions: Any, base_url: str = "https://llm.test/v1/"
    ) -> None:
        self.chat = SimpleNamespace(completions=completions)
        self.completions_impl = completions
        self.base_url = base_url


def _messages(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


def test_the_usage_observer_counts_and_passes_the_response_through() -> None:
    completions = _SyncCompletions()
    client = _Client(completions)
    usage = adapters.LLMUsage("https://llm.test/v1")
    usage.observe(client, "llm")
    with usage.attribute("add", "a.f1"):
        first = client.chat.completions.create(
            model="priced", messages=_messages("one two three")
        )
    assert first is completions.returned[-1]
    with usage.attribute("search", "t01"):
        client.chat.completions.create(model="plain", messages=_messages("four five"))
    with usage.attribute("add", "a.f2"), pytest.raises(httpx.HTTPStatusError):
        client.chat.completions.create(model="broken", messages=_messages("six"))
    completions.http.close()
    report = usage.report()
    assert report["observed"] is True and report["clients"] == {
        "llm": "https://llm.test/v1/"
    }
    assert report["totals"] == {
        "calls": 3,
        "failed": 1,
        "without_usage": 0,
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "total_tokens": 9,
        "cost": 0.25,
        "calls_with_cost": 1,
        "models": {"served/priced": 1, "served/plain": 1},
    }
    assert report["per_add"]["a.f1"] == {
        "calls": 1,
        "failed": 0,
        "without_usage": 0,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "cost": 0.25,
        "calls_with_cost": 1,
    }
    assert (
        report["per_add"]["a.f2"]["failed"] == 1
        and report["per_add"]["a.f2"]["cost"] is None
    )
    assert set(report["by_phase"]) == {"add", "search"}


def test_the_usage_observer_follows_an_async_client() -> None:
    completions = _AsyncCompletions()
    client = _Client(completions)
    usage = adapters.LLMUsage("https://llm.test/v1")
    usage.observe(client, "llm")

    async def go() -> Any:
        with usage.attribute("add", "b.f1"):
            response = await client.chat.completions.create(
                model="priced", messages=_messages("a b")
            )
        await completions.http.aclose()
        return response

    response = asyncio.run(go())
    assert response is completions.returned[-1]
    row = usage.report()["per_add"]["b.f1"]
    assert (row["calls"], row["prompt_tokens"], row["cost"]) == (1, 2, 0.25)


def test_the_arms_observe_their_own_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert adapters.Mem0Adapter(tmp_path, "raw").llm_usage is None
    remote = adapters.Mem0Adapter(tmp_path, "infer")
    remote._memory = SimpleNamespace(
        llm=SimpleNamespace(
            client=_Client(_SyncCompletions(), "https://openrouter.ai/api/v1/")
        )
    )
    monkeypatch.setattr(adapters, "REMOTE_LLM", True)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    remote._observe_llm(remote.llm_usage)
    assert remote.llm_usage.clients == {"mem0 llm": "https://openrouter.ai/api/v1/"}
    assert remote.llm_usage.notes == []
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-another-key")
    overridden = adapters.Mem0Adapter(tmp_path, "infer")
    overridden._memory = remote._memory
    overridden._observe_llm(overridden.llm_usage)
    notes = overridden.llm_usage.report()["notes"]
    assert "OPENROUTER_API_KEY is set" in notes[
        0
    ] and "sk-or-another-key" not in json.dumps(notes)
    monkeypatch.setattr(adapters, "REMOTE_LLM", False)
    local = adapters.Mem0Adapter(tmp_path, "infer")
    local._observe_llm(local.llm_usage)
    report = local.llm_usage.report()
    assert report["observed"] is False and "ollama" in report["reason"]
    graph = adapters.GraphitiAdapter(tmp_path)
    graph._g = SimpleNamespace(
        llm_client=SimpleNamespace(client=_Client(_AsyncCompletions())),
        cross_encoder=SimpleNamespace(client=_Client(_AsyncCompletions())),
    )
    graph._observe_llm(graph.llm_usage)
    assert set(graph.llm_usage.clients) == {"graphiti llm", "graphiti reranker"}
    bare = adapters.GraphitiAdapter(tmp_path)
    bare._g = SimpleNamespace()
    bare._observe_llm(bare.llm_usage)
    assert bare.llm_usage.report()["observed"] is False


KEY = "sk-or-test-key-never-in-an-artifact"
LABEL = "sk-or-v1-abc...xyz"


class _FakeOpenRouter(urllib.request.BaseHandler):
    """urllib's transport for the test: answers GET /api/v1/key from a
    script of replies (a JSON body, or an HTTP status to fail with) and
    refuses every other request, so nothing leaves the process."""

    handler_order = 0  # ahead of the real HTTP(S) handlers

    def __init__(self) -> None:
        self.replies: list[Any] = []
        self.seen: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request) -> Any:
        assert req.full_url == adapters.OPENROUTER_KEY_URL, req.full_url
        self.seen.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            raise urllib.error.HTTPError(
                req.full_url,
                reply,
                "refused",
                email.message.Message(),
                io.BytesIO(b"{}"),
            )
        response = urllib.response.addinfourl(
            io.BytesIO(json.dumps(reply).encode()),
            email.message.Message(),
            req.full_url,
            200,
        )
        response.msg = "OK"  # type: ignore[attr-defined]
        return response

    def http_open(self, req: urllib.request.Request) -> Any:
        raise AssertionError(f"unexpected request to {req.full_url}")


@pytest.fixture
def openrouter() -> Iterator[_FakeOpenRouter]:
    handler = _FakeOpenRouter()
    urllib.request.install_opener(urllib.request.build_opener(handler))
    try:
        yield handler
    finally:
        urllib.request.install_opener(None)


def test_the_key_read_sends_the_key_once_and_keeps_only_the_usage(
    monkeypatch: pytest.MonkeyPatch, openrouter: _FakeOpenRouter
) -> None:
    monkeypatch.setenv("BM_INTEGRITY_LLM_API_KEY", KEY)
    openrouter.replies = [
        {"data": {"label": LABEL, "usage": 1.25, "limit": 10}},
        401,
        {"data": {"label": LABEL}},
    ]
    assert adapters.openrouter_key_usage() == {"usage": 1.25}
    assert adapters.openrouter_key_usage() == {"error": "HTTP 401"}
    assert adapters.openrouter_key_usage() == {
        "error": "the key response carries no numeric data.usage"
    }
    assert [r.get_header("Authorization") for r in openrouter.seen] == [
        f"Bearer {KEY}"
    ] * 3
    monkeypatch.delenv("BM_INTEGRITY_LLM_API_KEY")
    assert adapters.openrouter_key_usage() == {
        "error": "BM_INTEGRITY_LLM_API_KEY is not set"
    }
    assert len(openrouter.seen) == 3
    assert adapters.key_usage_delta({"usage": 1.25}, {"usage": 1.5})["delta"] == 0.25
    assert "delta" not in adapters.key_usage_delta(
        {"error": "HTTP 401"}, {"usage": 1.5}
    )
    assert adapters.is_openrouter("https://openrouter.ai/api/v1")
    assert not adapters.is_openrouter("http://localhost:11434/v1")


def test_collect_records_an_llm_arms_usage_and_never_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openrouter: _FakeOpenRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BM_INTEGRITY_LLM_API_KEY", KEY)
    openrouter.replies = [
        {"data": {"label": LABEL, "usage": 1.25}},
        {"data": {"label": LABEL, "usage": 1.5}},
    ]
    arm = FakeArm(llm_endpoint="https://openrouter.ai/api/v1")
    _use(monkeypatch, arm)
    out = tmp_path / "raw.json"
    assert (
        runner.collect(
            "fake",
            out,
            tmp_path / "scratch",
            None,
            corpus_path=DEV,
            seals=tmp_path / "SEALS.json",
        )
        == 0
    )
    text = out.read_text(encoding="utf-8")
    raw = json.loads(text)
    usage = raw["llm_usage"]
    adds = [a["stmt_id"] for a in raw["adds"]]
    assert usage["observed"] is True
    assert usage["clients"] == {"fake llm": "https://openrouter.ai/api/v1"}
    assert list(usage["per_add"]) == adds
    assert all(row["calls"] == 1 for row in usage["per_add"].values())
    assert usage["by_phase"]["reset"]["calls"] == 1
    assert usage["by_phase"]["add"]["calls"] == len(adds)
    assert usage["totals"]["calls"] == 1 + len(adds)
    assert usage["totals"]["cost"] == 0.25 * (1 + len(adds))
    assert usage["openrouter_key"]["before"] == {"usage": 1.25}
    assert usage["openrouter_key"]["delta"] == 0.25
    assert [r.get_header("Authorization") for r in openrouter.seen] == [
        f"Bearer {KEY}"
    ] * 2
    captured = capsys.readouterr()
    for leak in (KEY, LABEL):
        assert leak not in text
        assert leak not in captured.out and leak not in captured.err
