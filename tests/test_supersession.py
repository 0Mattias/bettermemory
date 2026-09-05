"""Write-time supersession: the detector, its measured counts on the
sealed integrity corpus, and the write path that sets the links and
files the pairs.

The counts pinned here are the measurement the module docstring of
`bettermemory.supersession` cites. They move only with the corpus
(sealed by sha; `test_bench_integrity` pins it) or with a rule change,
and a rule change has to re-measure them in the same commit.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.conflicts import ConflictQueue
from bettermemory.events import Recorder, iter_events
from bettermemory.models import Confidence, Memory, Source, generate_ulid, snippet_for
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.search import _raw_content_token_set, tokenize
from bettermemory.store import Store
from bettermemory.supersession import (
    MAX_LINKS_PER_WRITE,
    SupersessionMatch,
    anchored_values,
    change_cues,
    claim_sized,
    detect_supersession,
    value_tokens,
    values_are_kin,
)

from ._mcp import call_tool as _mcp_call

_T = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ROOT = Path(__file__).resolve().parents[1]
_BENCH = _ROOT / "bench" / "integrity"

# Statements from the sealed corpus, quoted so a reader of this file sees
# the shapes the rule is judged on. `test_the_corpus_counts_are_pinned`
# replays the whole corpus; these pick out one case per rule.
_PORT_OLD = (
    "The auth service listens on port 8443 inside the cluster; the ingress "
    "terminates TLS and forwards to that port."
)
_PORT_NEW = (
    "The auth service moved to port 9443 when the sidecar was introduced; the "
    "ingress forwards to 9443 and the old port is closed."
)
_TRAIN_OLD = (
    "The weekly release train leaves on Tuesday; anything merged after the cut "
    "waits a week."
)
_TRAIN_NEW = (
    "The release train moved to Thursday so that fixes from the Monday support "
    "review make the same week's release."
)
_CANARY_OLD = (
    "The api gateway canary is watched on the gateway-canary-v1 dashboard "
    "during every rollout."
)
_CANARY_REPLACED = (
    "The canary dashboard was replaced by gateway-canary-v2, which adds "
    "per-region error panels."
)
_CANARY_BACK = (
    "The replacement dashboard was withdrawn after its queries proved too "
    "slow; rollouts are watched on gateway-canary-v1 again."
)
_INVOICE_OLD = (
    "Invoice PDFs in the billing service are rendered with wkhtmltopdf from an "
    "HTML template."
)
_INVOICE_DISTRACTOR = (
    "Invoice PDFs are archived for seven years under the finance retention "
    "policy, and the archive is read-only."
)
# A cue-less restatement with a different port, below the duplicate band.
_PORT_SILENT = (
    "The auth service accepts traffic on port 9443 from the ingress, which "
    "forwards after terminating TLS."
)


def _memory(body: str, scopes: tuple[str, ...] = ("infrastructure",)) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_T,
        updated=_T,
        scopes=list(scopes),
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _stem(word: str) -> str:
    """The token the shared tokenizer makes of a word — the detector reads
    stems, so a test that names a value compares the same way."""
    return tokenize(word)[0]


# ---------------------------------------------------------------------------
# Per-body features
# ---------------------------------------------------------------------------


def test_claim_sized_bounds() -> None:
    assert claim_sized(_PORT_OLD)
    assert not claim_sized("")
    six_sentences = " ".join(f"Sentence number {i} says a thing." for i in range(6))
    assert not claim_sized(six_sentences)
    long_body = "One claim. " + " ".join(f"token{i}" for i in range(100))
    assert not claim_sized(long_body)
    # Semicolons and colons are clause breaks, not sentence ends.
    assert claim_sized("a: b; c: d; e; f; g; h; i.")


def test_change_cues_in_order_and_on_word_boundaries() -> None:
    assert change_cues(_PORT_NEW) == ["moved", "the old"]
    assert change_cues("Skim the news feed; the renowned tool is unchanged.") == []
    assert change_cues("Paging moved to Opsgenie.") == ["moved"]


def test_anchored_values_follow_the_preposition_or_the_object() -> None:
    assert "port" in anchored_values("The auth service moved to port 9443.")
    assert _stem("biome") in anchored_values(
        "The web repo adopted biome as its formatter."
    )
    assert "yarn" in anchored_values("The web monorepo switched to yarn workspaces.")
    # No value slot in the clause: nothing is read off it.
    assert anchored_values(_CANARY_BACK) == set()


def test_value_tokens_are_numbers_compounds_and_proper_nouns() -> None:
    body = "Crashes from the courier app are reported to Sentry on port 8443 via deploy-gateway."
    values = value_tokens(body, _raw_content_token_set(body))
    assert {_stem("Sentry"), "8443", _stem("deploy-gateway")} <= values
    # A sentence-initial capital is sentence case, not a name; plain words
    # are not values.
    assert _stem("Crashes") not in values and _stem("courier") not in values


def test_values_are_kin_by_shape() -> None:
    assert values_are_kin("8443", "9443")
    assert values_are_kin("3.11", "3.13")
    assert not values_are_kin("1200", "429")
    assert not values_are_kin("8443", "3.13")
    assert values_are_kin("runners-medium", "runners-large")
    assert values_are_kin("hlx-exports-v2", "hlx-exports-prod")
    assert values_are_kin("eu-west-1", "eu-central-1")
    assert not values_are_kin("prod-hlx-1", "hlx-exports-v2")
    assert not values_are_kin("airflow", "dagster")
    assert not values_are_kin("8443", "8443")


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def test_cue_with_kin_values_supersedes() -> None:
    old = _memory(_PORT_OLD)
    report = detect_supersession(_PORT_NEW, [old])
    assert report.eligible
    assert [m.memory_id for m in report.supersedes] == [old.id]
    match = report.supersedes[0]
    assert (match.evidence, match.cue) == ("kin", "moved")
    assert (match.new_value, match.old_value) == ("9443", "8443")
    assert (
        match.note()
        == "set at write time: 'moved' in this memory, '9443' against '8443' (kin)"
    )
    assert match.detector() == "numeric"
    assert report.conflicts == []


def test_cue_with_context_evidence_supersedes() -> None:
    old = _memory(_TRAIN_OLD)
    report = detect_supersession(_TRAIN_NEW, [old])
    assert [m.memory_id for m in report.supersedes] == [old.id]
    match = report.supersedes[0]
    assert match.evidence == "context"
    assert (match.new_value, match.old_value) == ("Thursday", "Tuesday")
    assert match.detector() == "value"


def test_no_cue_with_high_overlap_is_a_conflict() -> None:
    old = _memory(_PORT_OLD)
    report = detect_supersession(_PORT_SILENT, [old])
    assert report.supersedes == []
    assert [m.memory_id for m in report.conflicts] == [old.id]
    assert report.conflicts[0].cue is None
    assert report.conflicts[0].detector() == "numeric"


def test_a_reversion_that_agrees_with_its_target_is_not_linked() -> None:
    first = _memory(_CANARY_OLD)
    replaced = _memory(_CANARY_REPLACED)
    report = detect_supersession(_CANARY_BACK, [first, replaced])
    assert [m.memory_id for m in report.supersedes] == [replaced.id]
    assert report.conflicts == []


def test_a_distractor_on_the_same_subject_sets_nothing() -> None:
    report = detect_supersession(_INVOICE_DISTRACTOR, [_memory(_INVOICE_OLD)])
    assert report.eligible
    assert report.supersedes == [] and report.conflicts == []


def test_long_bodies_are_not_candidates_on_either_side() -> None:
    record = " ".join(f"The service moved to port {9000 + i}." for i in range(8))
    assert not detect_supersession(record, [_memory(_PORT_OLD)]).eligible
    report = detect_supersession(_PORT_NEW, [_memory(record)])
    assert report.eligible and report.supersedes == []


def test_declared_targets_are_excluded_from_detection() -> None:
    old = _memory(_PORT_OLD)
    report = detect_supersession(_PORT_NEW, [old], exclude_ids=[old.id])
    assert report.supersedes == []


def test_links_are_capped_and_ordered_by_similarity() -> None:
    olds = [
        _memory(f"The auth service listens on port {8400 + i} inside the cluster.")
        for i in range(MAX_LINKS_PER_WRITE + 2)
    ]
    report = detect_supersession(_PORT_NEW, olds)
    assert len(report.supersedes) == MAX_LINKS_PER_WRITE
    similarities = [m.similarity for m in report.supersedes]
    assert similarities == sorted(similarities, reverse=True)


# ---------------------------------------------------------------------------
# The sealed corpus, replayed in its ingestion order
# ---------------------------------------------------------------------------


def _load_score() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "integrity_score", _BENCH / "score.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def replay() -> dict[str, Any]:
    """Every legitimate statement written in the benchmark's order, each
    judged against what was stored before it; then every poison payload
    judged against the whole legitimate set, as Phase C writes them."""
    score = _load_score()
    corpus = json.loads((_BENCH / "corpus.json").read_text(encoding="utf-8"))
    topics = {t["id"]: t for t in corpus["topics"]}
    existing: list[Memory] = []
    meta: dict[str, dict[str, Any]] = {}
    legit: list[tuple[dict[str, Any], SupersessionMatch]] = []
    for row in score.ingestion_plan(corpus):
        report = detect_supersession(row["text"], existing)
        legit.extend((row, m) for m in report.supersedes + report.conflicts)
        memory = _memory(row["text"])
        meta[memory.id] = row
        existing.append(memory)
    poison: list[tuple[dict[str, Any], SupersessionMatch]] = []
    for payload in corpus["poison"]:
        report = detect_supersession(payload["text"], existing)
        poison.extend((payload, m) for m in report.supersedes + report.conflicts)
    return {"topics": topics, "meta": meta, "legit": legit, "poison": poison}


def _label(topics: dict[str, Any], new: dict[str, Any], old: dict[str, Any]) -> str:
    if new["topic"] is None or old["topic"] is None:
        return "hard-negative"
    if new["topic"] != old["topic"]:
        return "cross-topic"
    kind = topics[new["topic"]]["kind"]
    pair = (new["role"], old["role"])
    if kind == "supersession":
        return "replaced" if pair == ("f2", "f1") else "same-topic"
    if kind == "reversion":
        if pair in {("f2", "f1"), ("f3", "f2")}:
            return "replaced"
        return "agrees" if pair == ("f3", "f1") else "same-topic"
    return "distractor"


def test_the_corpus_counts_are_pinned(replay: dict[str, Any]) -> None:
    counts: Counter[tuple[str, str]] = Counter()
    linked: set[str] = set()
    for row, match in replay["legit"]:
        label = _label(replay["topics"], row, replay["meta"][match.memory_id])
        counts[(match.outcome, label)] += 1
        if label == "replaced":
            linked.add(f"{row['topic']}:{row['role']}:{match.outcome}")
    # Every edge lands on the statement it replaces; nothing else fires.
    assert dict(counts) == {("supersedes", "replaced"): 27, ("conflict", "replaced"): 1}
    supersession = [
        t for t, v in replay["topics"].items() if v["kind"] == "supersession"
    ]
    reversion = [t for t, v in replay["topics"].items() if v["kind"] == "reversion"]
    assert sum(f"{t}:f2:supersedes" in linked for t in supersession) == 15
    assert sum(f"{t}:f2:supersedes" in linked for t in reversion) == 7
    assert sum(f"{t}:f3:supersedes" in linked for t in reversion) == 5
    assert sum(f"{t}:f3:conflict" in linked for t in reversion) == 1


def test_the_poison_lever_is_pinned(replay: dict[str, Any]) -> None:
    """A false fact with a change cue and a kin value earns the link over
    the true fact (`p01` against both t01 statements); the cue-less
    false facts are filed as conflicts against their target topic; no
    payload touches another topic."""
    lever: Counter[tuple[str, str, bool]] = Counter()
    for payload, match in replay["poison"]:
        target = replay["meta"][match.memory_id]["topic"] == payload.get("target_topic")
        lever[(payload["class"], match.outcome, target)] += 1
    assert lever[("false_fact", "supersedes", True)] == 2
    assert lever[("false_fact", "conflict", True)] == 13
    assert not any(not target for (_, _, target) in lever)
    assert not any(cls == "instruction" for (cls, _, _) in lever)


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


def _build(memory_dir: Path, **behavior: Any) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True, **behavior),
    )
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=recorder
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _search(server: Any, query: str) -> list[dict[str, Any]]:
    return _unwrap(await _call(server, "memory_search", query=query, auto_scope=False))


def _hit(hits: list[dict[str, Any]], mid: str) -> dict[str, Any]:
    return next(h for h in hits if h["id"] == mid)


async def test_write_sets_the_link_and_search_renders_it(memory_dir: Path) -> None:
    server = _build(memory_dir)
    old = await _call(
        server, "memory_write", content=_PORT_OLD, scopes=["infrastructure"]
    )
    new = await _call(
        server, "memory_write", content=_PORT_NEW, scopes=["infrastructure"]
    )
    assert new["status"] == "committed"
    assert new["supersedes"] == [
        {
            "id": old["id"],
            "summary": snippet_for(_PORT_OLD, max_chars=100),
            "evidence": "kin",
            "new_value": "9443",
            "old_value": "8443",
            "cue": "moved",
        }
    ]
    assert "conflicts_filed" not in new and "hint" not in new

    shown = _unwrap(await _call(server, "memory_show", id=new["id"]))
    assert shown["links"] == [
        {
            "type": "supersedes",
            "target_id": old["id"],
            "note": "set at write time: 'moved' in this memory, '9443' against '8443' (kin)",
        }
    ]

    hits = await _search(server, "auth service port ingress")
    stale = _hit(hits, old["id"])
    assert [e["id"] for e in stale["superseded_by"]] == [new["id"]]
    assert stale["superseded_by"][0]["link_note"].startswith("set at write time")
    assert "superseded_by" not in _hit(hits, new["id"])

    events = [
        e
        for e in iter_events(memory_dir)
        if e.get("kind") == "write" and e.get("id") == new["id"]
    ]
    assert events and events[0]["supersedes"] == [old["id"]]
    assert events[0]["supersedes_detected"] == [old["id"]]
    assert "conflicts_filed" not in events[0]


async def test_cue_less_divergence_files_a_conflict(memory_dir: Path) -> None:
    server = _build(memory_dir)
    old = await _call(
        server, "memory_write", content=_PORT_OLD, scopes=["infrastructure"]
    )
    new = await _call(
        server, "memory_write", content=_PORT_SILENT, scopes=["infrastructure"]
    )
    assert new["status"] == "committed"
    assert "supersedes" not in new
    (row,) = new["conflicts_filed"]
    assert row["id"] == old["id"]
    assert (row["new_value"], row["old_value"]) == ("9443", "8443")
    assert "cue" not in row
    assert "memory_conflicts" in new["hint"]

    (pending,) = ConflictQueue(memory_dir).pending()
    assert pending.id == row["pair_id"]
    assert {pending.a_id, pending.b_id} == {new["id"], old["id"]}
    assert pending.detector == "numeric"

    listed = _unwrap(await _call(server, "memory_conflicts"))
    assert listed["pending_total"] == 1
    assert listed["pending"][0]["id"] == row["pair_id"]

    shown = _unwrap(await _call(server, "memory_show", id=new["id"]))
    assert shown.get("links", []) == []
    events = [
        e
        for e in iter_events(memory_dir)
        if e.get("kind") == "write" and e.get("id") == new["id"]
    ]
    assert events[0]["conflicts_filed"] == [row["pair_id"]]
    assert "supersedes" not in events[0]


async def test_declared_supersedes_sets_the_link(memory_dir: Path) -> None:
    server = _build(memory_dir)
    old = await _call(
        server,
        "memory_write",
        content="the auth subsystem validates JWT session tokens",
        scopes=["tools"],
    )
    new = await _call(
        server,
        "memory_write",
        content="unrelated replacement note xyzzy",
        scopes=["tools"],
        supersedes=[old["id"], old["id"]],
    )
    assert new["supersedes"] == [{"id": old["id"], "evidence": "declared"}]
    shown = _unwrap(await _call(server, "memory_show", id=new["id"]))
    assert shown["links"] == [
        {"type": "supersedes", "target_id": old["id"], "note": "declared at write time"}
    ]
    hits = await _search(server, "auth JWT session tokens")
    assert [e["id"] for e in _hit(hits, old["id"])["superseded_by"]] == [new["id"]]
    events = [
        e
        for e in iter_events(memory_dir)
        if e.get("kind") == "write" and e.get("id") == new["id"]
    ]
    assert events[0]["supersedes"] == [old["id"]]
    assert "supersedes_detected" not in events[0]


async def test_declared_supersedes_refuses_what_is_not_an_active_memory(
    memory_dir: Path,
) -> None:
    server = _build(memory_dir)
    old = await _call(
        server, "memory_write", content=_PORT_OLD, scopes=["infrastructure"]
    )
    with pytest.raises(Exception, match="not a memory id"):
        await _call(
            server,
            "memory_write",
            content="x y z",
            scopes=["tools"],
            supersedes=["nope"],
        )
    with pytest.raises(Exception, match="not an active memory"):
        await _call(
            server,
            "memory_write",
            content="x y z",
            scopes=["tools"],
            supersedes=[generate_ulid()],
        )
    await _call(server, "memory_remove", id=old["id"], reason="test")
    with pytest.raises(Exception, match="not an active memory"):
        await _call(
            server,
            "memory_write",
            content="x y z",
            scopes=["tools"],
            supersedes=[old["id"]],
        )


async def test_a_pending_write_sets_its_links_at_confirm(memory_dir: Path) -> None:
    server = _build(memory_dir, require_write_confirmation=True)
    staged = await _call(
        server, "memory_write", content=_PORT_OLD, scopes=["infrastructure"]
    )
    old = await _call(server, "memory_write_confirm", pending_id=staged["pending_id"])
    staged = await _call(
        server, "memory_write", content=_PORT_NEW, scopes=["infrastructure"]
    )
    assert staged["status"] == "pending" and "supersedes" not in staged
    new = await _call(server, "memory_write_confirm", pending_id=staged["pending_id"])
    assert new["status"] == "committed"
    assert [row["id"] for row in new["supersedes"]] == [old["id"]]
    hits = await _search(server, "auth service port ingress")
    assert [e["id"] for e in _hit(hits, old["id"])["superseded_by"]] == [new["id"]]
    events = [e for e in iter_events(memory_dir) if e.get("kind") == "write_confirm"]
    assert events[-1]["supersedes"] == [old["id"]]


async def test_a_forced_write_still_detects(memory_dir: Path) -> None:
    server = _build(memory_dir)
    old = await _call(
        server, "memory_write", content=_PORT_OLD, scopes=["infrastructure"]
    )
    forced = _PORT_OLD.replace(
        "service listens on port 8443", "service now listens on port 9443"
    )
    dup = await _call(server, "memory_write", content=forced, scopes=["infrastructure"])
    assert dup["status"] == "duplicate"
    new = await _call(
        server, "memory_write", content=forced, scopes=["infrastructure"], force=True
    )
    assert new["status"] == "committed"
    assert [row["id"] for row in new["supersedes"]] == [old["id"]]


async def test_the_flag_off_leaves_links_to_the_writer(memory_dir: Path) -> None:
    server = _build(memory_dir, write_supersession=False)
    old = await _call(
        server, "memory_write", content=_PORT_OLD, scopes=["infrastructure"]
    )
    new = await _call(
        server, "memory_write", content=_PORT_NEW, scopes=["infrastructure"]
    )
    assert "supersedes" not in new and "conflicts_filed" not in new
    assert (
        _unwrap(await _call(server, "memory_show", id=new["id"])).get("links", []) == []
    )
    declared = await _call(
        server,
        "memory_write",
        content="a different note about the gateway canary dashboard",
        scopes=["infrastructure"],
        supersedes=[old["id"]],
    )
    assert declared["supersedes"] == [{"id": old["id"], "evidence": "declared"}]
