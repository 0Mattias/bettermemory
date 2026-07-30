"""Tests for `bench/dedup/run.py` and the regime it measured.

The bench exists to answer one question — would lowering the dedup
`related` floor from 0.40 to 0.30 surface the paraphrase pairs the
2026-07-30 audit found sitting at Jaccard 0.17-0.33? — and the answer it
returned was no, for a reason that is arithmetic rather than empirical.
These tests pin that reason, because it is the kind of finding that
otherwise survives only as a sentence in a README and gets re-litigated
by the next person who reads the constant and thinks it is one knob.

`_pairwise_content_jaccard` returns `max(jaccard, containment)` once
containment clears `MEDIUM_SIMILARITY`, and containment
(|intersection| / |smaller|) is never below Jaccard
(|intersection| / |union|). Tightened: a pair whose Jaccard reaches `f`
has containment at least `2f / (1 + f)`. That bound equals 0.40 exactly
at f = 0.25, so every reported floor at or above 0.25 is unreachable —
whatever it would newly admit, containment already lifted into the band.
The only regime where the floor still bites is a smaller side under
`_CONTAINMENT_MIN_TOKENS`, where containment never fires at all.

So the two live edits behave nothing like each other, and neither does
what the item asked for:

- passing a lower `medium_threshold` is inert above 0.25;
- editing `MEDIUM_SIMILARITY` moves the containment GATE too, which
  admits pairs whose real token overlap is a few percent — the opposite
  of the paraphrase rescue that motivated it.

`bench/` is not a package and is not on the import path, so the module is
loaded by file location the same way a bench run would execute it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory import search as search_mod
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _CONTAINMENT_MIN_TOKENS,
    HIGH_SIMILARITY,
    MEDIUM_SIMILARITY,
    find_similar,
)

_ROOT = Path(__file__).resolve().parents[1]
_RUN = _ROOT / "bench" / "dedup" / "run.py"
_ARTIFACT = _ROOT / "bench" / "dedup" / "results" / "live-store-2026-07-30.json"

# Where the containment lift stops covering for the reported floor:
# 2f/(1+f) >= MEDIUM  <=>  f >= MEDIUM / (2 - MEDIUM).
_BREAKPOINT = MEDIUM_SIMILARITY / (2 - MEDIUM_SIMILARITY)


def _load() -> ModuleType:
    # Its own sys.modules key — the other bench suites register their
    # run.py under keys of their own, and colliding keys would hand one
    # suite another's module object.
    spec = importlib.util.spec_from_file_location("bench_dedup_run", _RUN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_dedup_run"] = module
    spec.loader.exec_module(module)
    return module


bench = _load()


def _body(prefix: str, count: int, *, shared: int = 0) -> str:
    """A body of `count` distinct non-stopword tokens, the first `shared`
    of which are drawn from a common pool. No hyphens, so the pairwise
    kebab expansion is a no-op and the token counts are exactly what the
    arithmetic below assumes."""
    tokens = [f"common{i:04d}" for i in range(shared)]
    tokens += [f"{prefix}{i:04d}" for i in range(count - shared)]
    return " ".join(tokens)


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["bench"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _relevance(new_body: str, other: str, *, floor: float) -> str | None:
    hits = find_similar(new_body, [_memory(other)], medium_threshold=floor)
    return hits[0].relevance if hits else None


# --------------------------------------------------------------------------
# The structural result: above the breakpoint, the floor is not a knob
# --------------------------------------------------------------------------


def test_the_breakpoint_matches_the_shipped_constants() -> None:
    """The bench reports the breakpoint; it must be derived, not typed."""
    assert _BREAKPOINT == pytest.approx(0.25)
    assert bench.containment_dominance([_memory(_body("a", 20))])[
        "floor_below_which_the_knob_can_bite"
    ] == pytest.approx(_BREAKPOINT, abs=1e-4)


def test_a_lower_floor_admits_nothing_new_above_the_breakpoint() -> None:
    """Sweep the (|A|, |B|, |shared|) space: whenever raw Jaccard reaches
    0.25 and the smaller side can reach the containment gate, the pair is
    ALREADY `medium` at the shipped 0.40 floor.

    This is the whole negative result. If it ever fails, the containment
    lift has been narrowed and the floor becomes a real knob again — at
    which point F9 is worth re-measuring rather than re-closed.
    """
    checked = 0
    for size_a in (10, 16, 24, 40):
        for size_b in (10, 16, 24, 40, 80):
            for shared in range(1, min(size_a, size_b) + 1):
                union = size_a + size_b - shared
                jaccard = shared / union
                if jaccard < _BREAKPOINT or jaccard >= HIGH_SIMILARITY:
                    continue
                if min(size_a, size_b) < _CONTAINMENT_MIN_TOKENS:
                    continue
                a = _body("aa", size_a, shared=shared)
                b = _body("bb", size_b, shared=shared)
                assert _relevance(a, b, floor=MEDIUM_SIMILARITY) == "medium", (
                    f"|A|={size_a} |B|={size_b} shared={shared} "
                    f"jaccard={jaccard:.4f} is not in the band at the "
                    f"shipped floor, so a 0.30 floor would newly admit it"
                )
                checked += 1
    assert checked > 100, "the sweep degenerated; it is no longer a pin"


def test_below_the_breakpoint_the_floor_starts_biting_again() -> None:
    """The counterpart that keeps the test above from being vacuous.

    Equal-sized sets sharing a third of their tokens score Jaccard 0.20
    and containment 0.333 — under the 0.40 containment gate, so the raw
    Jaccard is the score and the reported floor decides. A 0.20 floor
    surfaces it; 0.25 and up do not.
    """
    a = _body("aa", 15, shared=5)
    b = _body("bb", 15, shared=5)
    assert _relevance(a, b, floor=0.20) == "medium"
    assert _relevance(a, b, floor=0.25) is None
    assert _relevance(a, b, floor=0.30) is None
    assert _relevance(a, b, floor=MEDIUM_SIMILARITY) is None


def test_short_bodies_are_the_only_regime_a_030_floor_changes() -> None:
    """Under `_CONTAINMENT_MIN_TOKENS` the containment lift never fires,
    so raw Jaccard in [0.30, 0.40) survives and a 0.30 floor does surface
    it. The live store measured ZERO memories that short, which is why
    the measured delta was zero rather than small."""
    short = _CONTAINMENT_MIN_TOKENS - 2
    a = _body("aa", short, shared=3)
    b = _body("bb", short, shared=3)
    assert _relevance(a, b, floor=0.30) == "medium"
    assert _relevance(a, b, floor=MEDIUM_SIMILARITY) is None


# --------------------------------------------------------------------------
# The naive edit: what moving the constant actually does
# --------------------------------------------------------------------------


def test_lowering_the_constant_admits_pairs_far_below_the_new_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test that would have caught "just set MEDIUM_SIMILARITY = 0.30".

    `MEDIUM_SIMILARITY` is read in three places, one of which is the gate
    that lets containment fire. Lowering it does not admit 0.30-Jaccard
    paraphrases — it admits long-versus-short pairs whose real overlap is
    a few percent, because containment is generous exactly where Jaccard
    is not. The pair below shares 20 tokens across bodies of 60 and 300:
    containment 0.333, actual Jaccard 0.059.
    """
    a = _body("aa", 60, shared=20)
    b = _body("bb", 300, shared=20)
    jaccard = 20 / (60 + 300 - 20)
    assert jaccard == pytest.approx(0.0588, abs=1e-3)

    assert _relevance(a, b, floor=MEDIUM_SIMILARITY) is None

    monkeypatch.setattr(search_mod, "MEDIUM_SIMILARITY", 0.30)
    monkeypatch.setattr(
        search_mod, "_CONTAINMENT_CEILING", (HIGH_SIMILARITY + 0.30) / 2
    )
    assert _relevance(a, b, floor=0.30) == "medium", (
        "the constant edit was supposed to be inert here; if it is, the "
        "containment gate no longer reads MEDIUM_SIMILARITY and the "
        "bench's naive arm is measuring the wrong thing"
    )


def test_naive_edit_helper_restores_the_globals_it_rebinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bench arms rebind globals of the SHIPPED search module. A leak
    would silently retune dedup for every later caller in the process —
    including the rest of a bench run, whose remaining arms would then
    report against the wrong baseline. Restoration must survive an
    exception, not just the happy path."""

    def _globals() -> tuple[float, float, int]:
        return (
            search_mod.MEDIUM_SIMILARITY,
            search_mod._CONTAINMENT_CEILING,
            search_mod._CONTAINMENT_MIN_TOKENS,
        )

    pair = [_memory(_body("aa", 20, shared=8)), _memory(_body("bb", 20, shared=8))]
    before = _globals()
    bench._naive_edit(pair, 0.30)
    assert _globals() == before
    # `naive_admits_what` rebinds a third global (`_CONTAINMENT_MIN_TOKENS`,
    # to read unlifted Jaccard). Leaking that one disables the containment
    # branch for every later caller in the process.
    bench.naive_admits_what(pair, 0.30)
    assert _globals() == before

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("arm failed mid-sweep")

    monkeypatch.setattr(bench, "_scan", _boom)
    with pytest.raises(RuntimeError):
        bench._naive_edit(pair, 0.30)
    assert _globals() == before


# --------------------------------------------------------------------------
# The artifact: numbers only, because the corpus is someone's real store
# --------------------------------------------------------------------------


def test_report_carries_no_memory_content() -> None:
    """The committed artifact was produced from a private store. The
    report must be counts only — a regression that started attaching
    snippets, ids or paths for context would exfiltrate the corpus into
    a public repo on the next run."""
    secret = "zzsecretzz"
    memories = [
        _memory(f"{secret} {_body('aa', 30, shared=10)}"),
        _memory(f"{secret} {_body('bb', 30, shared=10)}"),
    ]
    report = bench.measure(memories, [], "unit")
    blob = json.dumps(report)
    assert secret not in blob
    for mem in memories:
        assert mem.id not in blob


def test_committed_artifact_leaks_neither_bodies_nor_the_store_path() -> None:
    """Same rule applied to what is actually on disk. The live arm was run
    against the author's real store; a home directory or a snippet reaching
    the committed JSON is the failure that matters, and it is invisible in
    review because the file is a wall of numbers."""
    text = _ARTIFACT.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "/Users" not in text and "/home/" not in text
    assert data["live_store"]["corpus"] == "live store"

    def _leaves(node: object) -> list[object]:
        if isinstance(node, dict):
            return [x for v in node.values() for x in _leaves(v)]
        if isinstance(node, list):
            return [x for v in node for x in _leaves(v)]
        return [node]

    # Every measured leaf is a number. Prose lives only in the hand-written
    # wrapper keys, which are enumerated here so a new free-text field has
    # to be added deliberately.
    prose_keys = {"date", "bettermemory_version", "corpus", "corpus_note"}
    for arm in ("live_store", "public_corpus"):
        stripped = {k: v for k, v in data[arm].items() if k not in prose_keys}
        assert all(
            isinstance(leaf, (int, float)) or leaf is None for leaf in _leaves(stripped)
        ), f"{arm} carries a non-numeric leaf"


def test_committed_artifact_records_the_decision_inputs() -> None:
    """The artifact is the evidence the item was closed on. Keep the keys
    a re-reader needs to check the reasoning without re-running it."""
    data = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert data["decision"] == "closed-negative"
    live = data["live_store"]
    sweep = live["surgical_floor_sweep"]
    baseline = sweep[f"{MEDIUM_SIMILARITY:.2f}"]
    # The finding itself: identical load at every floor above the
    # breakpoint. Stated as an assertion so a re-run that contradicts it
    # cannot be committed while the README still says it holds.
    for floor in ("0.40", "0.35", "0.30", "0.25"):
        assert sweep[floor] == baseline
    assert sweep["0.20"] != baseline
    naive = live["naive_constant_edit"]
    assert (
        naive["MEDIUM_SIMILARITY=0.30"]["related_pairs"]
        > 4 * naive["MEDIUM_SIMILARITY=0.40"]["related_pairs"]
    )
    # The sentence the README leads with, and the one that closes the item:
    # the constant edit admits thousands of hits and not one of them is a
    # 0.30-Jaccard pair.
    admits = live["naive_admits_what"]
    assert admits["added_hits"] > 1000
    assert admits["added_hits_at_or_above_the_new_floor"] == 0
    assert admits["raw_jaccard_quartiles"]["max"] < 0.30
    # The report floor is not what changed the numbers, so the shipped
    # constants must still be the ones the closed form was written about.
    assert (MEDIUM_SIMILARITY, _CONTAINMENT_MIN_TOKENS) == (0.40, 8)
    assert live["containment_dominance"]["memories_below_containment_min_tokens"] == 0
    # The item's premise, checked rather than argued: every pair sitting in
    # the band the proposal aimed at is already breadcrumbed.
    band = live["target_band_already_surfaced"]
    assert (
        band["of_those_already_surfaced_as_related"]
        == band["hits_with_raw_jaccard_in_target_band"]
    )
