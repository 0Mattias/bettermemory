"""Tests for `bench/heldout/run.py`, the held-out instrument's container.

The instrument's content is authored independently under the seal in
`bench/heldout/FORMAT.md`; this file tests the container that will hold
it. That ordering is the reason the tests matter: the harness has to be
proven correct BEFORE any content exists, because once the instrument
lands the implementer may not read it, and a harness bug found later
could not be diagnosed against the real data without breaking the seal.

Two properties carry the most weight and are tested hardest:

1. **Validation rejects what it must.** A malformed instrument that
   loads anyway is worse than one that fails, because it scores.
2. **The label never enters the searchable content.** A session id
   reaching a memory body would make the gold answer retrievable as
   content and the instrument would measure the leak.

Everything runs against `bench/heldout/fixtures/`, obviously-fake
placeholder material that is never part of an instrument.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "heldout"
_FIXTURES = _BENCH / "fixtures"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_heldout", _BENCH / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_heldout"] = module
    spec.loader.exec_module(module)
    return module


harness = _load()


@pytest.fixture
def personas() -> list[dict[str, Any]]:
    return json.loads((_FIXTURES / "personas.json").read_text(encoding="utf-8"))


@pytest.fixture
def questions() -> list[dict[str, Any]]:
    return json.loads((_FIXTURES / "questions.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The fixture is a fixture, not an instrument
# ---------------------------------------------------------------------------


def test_the_fixture_declares_itself_not_an_instrument() -> None:
    """A fixture mistaken for the instrument would be scored and
    published. Both the manifest and a README say what it is."""
    manifest = json.loads((_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["instrument"] == "harness-fixture"
    assert manifest["sealed"] is False
    assert "not an instrument" in (_FIXTURES / "README.md").read_text().lower()


def test_the_landed_instrument_is_loadable_and_sealed() -> None:
    """Post-landing form of the tripwire. The instrument landed in
    `35227dd`, so the seal in FORMAT.md is in force from that commit
    until the preregistered run.

    **This test is deliberately blind.** It asserts existence, mechanical
    loadability and the seal flag; it never renders question text, gold
    labels, or persona content, and it must not be extended to. The
    harness loading files programmatically is fine — that is what the
    seal permits and what `--validate` exists for. Printing any of it is
    not.
    """
    data = _BENCH / "data"
    assert data.exists(), "the instrument has not landed"
    for name in ("personas.json", "questions.json", "manifest.json"):
        assert (data / name).exists(), f"missing {name}"

    by_persona, questions, manifest = harness.load(data)
    # Counts and flags only — no content crosses this boundary.
    assert len(by_persona) >= 2
    assert len(questions) >= 1
    assert manifest["sealed"] is True, (
        "manifest does not assert the seal; the instrument cannot be used "
        "as a held-out check"
    )
    assert manifest["instrument"] != "harness-fixture"


# ---------------------------------------------------------------------------
# Validation accepts a well-formed instrument
# ---------------------------------------------------------------------------


def test_the_fixture_loads(
    personas: list[dict[str, Any]], questions: list[dict[str, Any]]
) -> None:
    by_persona = harness.validate_personas(personas)
    assert set(by_persona) == {"persona_1", "persona_2"}
    assert harness.validate_questions(questions, by_persona) == questions


def test_load_reads_all_three_files() -> None:
    by_persona, qs, manifest = harness.load(_FIXTURES)
    assert len(by_persona) == 2
    assert len(qs) == 3
    assert manifest["license"] == "MIT"


# ---------------------------------------------------------------------------
# Validation rejects what it must
# ---------------------------------------------------------------------------


def _mutate(personas: list[dict[str, Any]], fn: Any) -> list[dict[str, Any]]:
    out = copy.deepcopy(personas)
    fn(out)
    return out


@pytest.mark.parametrize(
    ("label", "mutate", "match"),
    [
        (
            "duplicate persona id",
            lambda p: p.append(copy.deepcopy(p[0])),
            "duplicate persona_id",
        ),
        (
            "duplicate session id across personas",
            lambda p: p[1]["sessions"][0].__setitem__("session_id", "persona_1_s1"),
            "duplicate session_id",
        ),
        (
            "single session",
            lambda p: p[0].__setitem__("sessions", p[0]["sessions"][:1]),
            ">= 2 sessions",
        ),
        (
            "bad id charset",
            lambda p: p[0].__setitem__("persona_id", "Persona-1"),
            "bad persona_id",
        ),
        (
            "bad date shape",
            lambda p: p[0]["sessions"][0].__setitem__("date", "05-01-2026"),
            "bad date",
        ),
        (
            "dates go backwards",
            lambda p: p[0]["sessions"][1].__setitem__("date", "2020-01-01"),
            "precedes the previous session",
        ),
        (
            "empty turns",
            lambda p: p[0]["sessions"][0].__setitem__("turns", []),
            ">= 1 turn",
        ),
        (
            "bad role",
            lambda p: p[0]["sessions"][0]["turns"][0].__setitem__("role", "system"),
            "bad role",
        ),
        (
            "blank turn text",
            lambda p: p[0]["sessions"][0]["turns"][0].__setitem__("text", "   "),
            "empty turn text",
        ),
        (
            "newline in a turn",
            lambda p: p[0]["sessions"][0]["turns"][0].__setitem__("text", "a\nb"),
            "contains a newline",
        ),
    ],
)
def test_malformed_personas_are_rejected(
    personas: list[dict[str, Any]], label: str, mutate: Any, match: str
) -> None:
    """An instrument that loads when it should not is worse than one
    that fails, because it produces a number."""
    with pytest.raises(harness.InstrumentError, match=match):
        harness.validate_personas(_mutate(personas, mutate))


def test_a_session_id_inside_turn_text_is_rejected(
    personas: list[dict[str, Any]],
) -> None:
    """The load-bearing content rule: the gold label must not be
    retrievable as content, or the instrument measures the leak."""
    bad = _mutate(
        personas,
        lambda p: p[0]["sessions"][0]["turns"][0].__setitem__(
            "text", "the answer is in persona_1_s2"
        ),
    )
    with pytest.raises(harness.InstrumentError, match="contains session id"):
        harness.validate_personas(bad)


@pytest.mark.parametrize(
    ("label", "mutate", "match"),
    [
        (
            "unknown persona",
            lambda q: q[0].__setitem__("persona_id", "persona_9"),
            "unknown persona_id",
        ),
        (
            "duplicate question id",
            lambda q: q.append(copy.deepcopy(q[0])),
            "duplicate question_id",
        ),
        (
            "no answers",
            lambda q: q[0].__setitem__("answer_session_ids", []),
            ">= 1 answer session",
        ),
        (
            "unknown answer session",
            lambda q: q[0].__setitem__("answer_session_ids", ["nope"]),
            "unknown answer session",
        ),
        (
            "answer belongs to another persona",
            lambda q: q[0].__setitem__("answer_session_ids", ["persona_2_s1"]),
            "belongs to persona_2",
        ),
        (
            "duplicate answers",
            lambda q: q[0].__setitem__(
                "answer_session_ids", ["persona_1_s1", "persona_1_s1"]
            ),
            "duplicate answer session ids",
        ),
        (
            "blank question",
            lambda q: q[0].__setitem__("question", " "),
            "empty question",
        ),
    ],
)
def test_malformed_questions_are_rejected(
    personas: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    label: str,
    mutate: Any,
    match: str,
) -> None:
    by_persona = harness.validate_personas(personas)
    bad = copy.deepcopy(questions)
    mutate(bad)
    with pytest.raises(harness.InstrumentError, match=match):
        harness.validate_questions(bad, by_persona)


# ---------------------------------------------------------------------------
# The envelope is a warning, not a gate
# ---------------------------------------------------------------------------


def test_envelope_deviations_warn_rather_than_fail(
    personas: list[dict[str, Any]], questions: list[dict[str, Any]]
) -> None:
    """A delivered instrument slightly outside the soft target is still
    scorable; refusing to load it would be the tail wagging the dog.
    The tiny fixture is outside on every axis, and still loads."""
    by_persona = harness.validate_personas(personas)
    warnings = harness.envelope_warnings(by_persona, questions)
    assert warnings, "the fixture is deliberately outside the envelope"
    assert any("persona count" in w for w in warnings)
    assert any("question count" in w for w in warnings)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_the_session_id_never_reaches_a_memory_body(
    tmp_path: Path, personas: list[dict[str, Any]]
) -> None:
    """The other half of the leak rule, on the write side."""
    from bettermemory.store import Store

    by_persona = harness.validate_personas(personas)
    id_to_session = harness.build_persona_store(tmp_path, by_persona["persona_1"])
    assert set(id_to_session.values()) == {
        "persona_1_s1",
        "persona_1_s2",
        "persona_1_s3",
    }
    for memory in Store(tmp_path).load_all():
        for sid in id_to_session.values():
            assert sid not in memory.body
        assert memory.scopes == harness.SCOPE


def test_distinct_sessions_collapses_first_occurrence_wins() -> None:
    mapping = {"m1": "s1", "m2": "s2", "m3": "s1"}
    assert harness.distinct_sessions(["m1", "m3", "m2"], mapping) == ["s1", "s2"]
    assert harness.distinct_sessions(["m9"], mapping) == []


def test_question_record_marks_unretrieved_evidence_as_null() -> None:
    q = {"question_id": "q_1", "persona_id": "p", "answer_session_ids": ["s1", "s2"]}
    rec = harness.question_record(q, ["s3", "s1"])
    assert rec["evidence_ranks"] == [1, None]
    assert rec["n_evidence"] == 2


def test_recall_and_ceiling_arithmetic() -> None:
    rec = {"n_evidence": 2, "evidence_ranks": [0, 7]}
    assert harness.recall_at(rec, 1) == 0.5
    assert harness.recall_at(rec, 5) == 0.5
    assert harness.recall_at(rec, 10) == 1.0
    # A 2-evidence question cannot exceed 0.5 at k=1, by construction.
    assert harness.ceiling_at(rec, 1) == 0.5
    assert harness.ceiling_at(rec, 5) == 1.0


def test_scoring_the_fixture_reproduces_from_its_own_sidecar(
    personas: list[dict[str, Any]], questions: list[dict[str, Any]]
) -> None:
    """The published macro figure must be recomputable from the
    per-question records alone — that is what makes the sidecar a
    receipt rather than a decoration."""
    by_persona = harness.validate_personas(personas)
    records, summary = harness.score(by_persona, questions)
    assert len(records) == len(questions)
    for k in harness.K_VALUES:
        recomputed = sum(harness.recall_at(r, k) for r in records) / len(records)
        assert summary["macro"][str(k)] == pytest.approx(round(recomputed, 4))


def test_retrieval_depth_exceeds_the_largest_k() -> None:
    assert harness.RETRIEVAL_DEPTH > max(harness.K_VALUES)


# ---------------------------------------------------------------------------
# The seal
# ---------------------------------------------------------------------------


def test_validate_is_separate_from_score() -> None:
    """`--validate` must be usable on a sealed instrument: it reads
    content only to check it and prints none of it. Scoring is what
    breaks the seal and is a different flag typed deliberately."""
    source = (_BENCH / "run.py").read_text(encoding="utf-8")
    assert "add_mutually_exclusive_group(required=True)" in source
    assert "--validate" in source and "--score" in source


def test_the_format_document_carries_the_seal_protocol() -> None:
    """The enforcement record is the ordering of three shas, and it is
    only enforceable if it is written down before the data lands."""
    doc = (_BENCH / "FORMAT.md").read_text(encoding="utf-8")
    assert "data commit  <  preregistration commit  <  run commit" in doc
    assert "no-read attestation" in doc


def test_the_format_document_gives_no_authoring_guidance() -> None:
    """The instrument is only worth building if its content was authored
    without knowledge of what the system under test finds hard. This
    pins the omission, because it is the kind of thing a later edit
    'helpfully' undoes.
    """
    doc = (_BENCH / "FORMAT.md").read_text(encoding="utf-8").lower()
    for leak in (
        "paraphrase",
        "synonym",
        "vocabulary gap",
        "casual",
        "colloquial",
        "inflection",
        "abbreviation",
        "rephrase",
    ):
        assert leak not in doc, (
            f"FORMAT.md mentions {leak!r} — that is authoring guidance about "
            f"the system under test, and it compromises the instrument"
        )
