"""End-to-end tests for the recent_negative_outcomes annotation on
memory_search hits (T2.3 of the 1.6 plan).

The wire contract: a hit gets a `recent_negative_outcomes` list when
the memory has been `ignored` or `contradicted` within the window
AND not since been `applied`. Negatives superseded by a later applied
event are filtered out — the user already validated the memory after
the rejection, so surfacing the rejection would be misleading.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder
from bettermemory.config import Config, StorageConfig
from bettermemory.events import EVENT_LOG_FILENAME, Recorder
from bettermemory.models import Confidence, MemoryHit
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def server_with_rec(memory_dir: Path) -> tuple[Any, Recorder]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )
    return server, rec


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _seed(server: Any, body: str, *, acknowledge_user_claim: bool = False) -> str:
    """Write one memory and hand back its id.

    `acknowledge_user_claim` defaults to False and is passed by exactly
    one caller. That asymmetry is deliberate: every other body here
    ("python list comprehension", "python decorators and closures") is
    a plain tooling note, and leaving them on the unacknowledged path
    means they keep proving that ordinary bodies do NOT trip
    `UserClaimGate`. Flipping the default for the whole helper would
    have made this file blind to a gate that started refusing
    everything.
    """
    res = await _call(
        server,
        "memory_write",
        content=body,
        scopes=["tools"],
        acknowledge_user_claim=acknowledge_user_claim,
    )
    return res["id"]


async def test_no_annotation_when_no_negative_events(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Baseline: a fresh memory with no use events should not carry
    a `recent_negative_outcomes` field. Byte-stable for the common
    case."""
    server, _ = server_with_rec
    await _seed(server, "python list comprehension notes")
    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    assert "recent_negative_outcomes" not in hits[0]


async def test_ignored_event_surfaces_as_annotation(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """A single ignored event within the window should produce a hit
    annotation with outcome='ignored', count_in_window=1, and the
    timestamp + session of the event."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension notes")

    await _call(
        server,
        "memory_record_use",
        memory_ids=[mid],
        outcome="ignored",
        note="not what I needed",
    )

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert len(annotations) == 1
    entry = annotations[0]
    assert entry["outcome"] == "ignored"
    assert entry["count_in_window"] == 1
    assert entry["note"] == "not what I needed"
    assert "most_recent_ts" in entry
    assert "session_id" in entry


async def test_applied_event_supersedes_earlier_ignored(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """The whole point of the supersession rule: if the user ignored a
    memory once but later applied it, the rejection is no longer
    actionable. Surfacing it would be misleading."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="applied")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert "recent_negative_outcomes" not in hits[0]


async def test_auto_applied_use_does_not_clear_contradiction(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Regression: the auto-`record_use` fallback emits outcome='applied' with
    auto=True purely because a re-surfaced use-token aged past its ~2-turn TTL
    — no model/user judgment. It must NOT supersede an earlier `contradicted`,
    or a memory the model explicitly flagged as WRONG silently loses its
    rejection warning the next time it's retrieved. Only a genuine (non-auto)
    applied event supersedes.
    """
    server, rec = server_with_rec
    mid = await _seed(server, "python list comprehension")

    # The model explicitly records the memory as contradicted (wrong).
    await _call(
        server,
        "memory_record_use",
        memory_ids=[mid],
        outcome="contradicted",
        note="user said this is wrong",
    )
    # The server's auto-commit fallback later fires with NO judgment — the
    # exact event the in-process _advance_turn writes when a re-surfaced
    # token lapses.
    rec.record("use", ids=[mid], outcome="applied", auto=True, attribution="auto")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None, (
        "an auto-applied use event must NOT clear a contradiction — the "
        "rejection warning has to survive an unattended auto-commit"
    )
    assert any(a["outcome"] == "contradicted" for a in annotations)


async def test_genuine_applied_still_supersedes_contradiction(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Complement: a real (non-auto) memory_record_use(applied) DOES still
    supersede a prior contradiction — the legitimate validation path must
    not regress when the auto guard is added."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="contradicted")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="applied")
    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert "recent_negative_outcomes" not in hits[0]


async def test_ignored_after_applied_does_surface(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Order matters in the supersession rule. Applied → ignored means
    the most recent signal is the rejection, which IS actionable."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="applied")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert len(annotations) == 1
    assert annotations[0]["outcome"] == "ignored"


async def test_contradicted_surfaces_as_separate_entry(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """`contradicted` is a distinct negative outcome from `ignored`.
    Both can appear on the same hit if neither has been superseded."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")
    await _call(server, "memory_record_use", memory_ids=[mid], outcome="contradicted")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    outcomes = {a["outcome"] for a in annotations}
    assert outcomes == {"ignored", "contradicted"}


async def test_corrected_outcome_does_not_surface(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """`corrected` is audit-only — the model already fixed the drift
    inline. It's a positive signal (memory was salvaged), not a
    rejection. Should not appear in the negative-outcomes list."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    await _call(server, "memory_record_use", memory_ids=[mid], outcome="corrected")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert "recent_negative_outcomes" not in hits[0]


async def test_count_in_window_reflects_multiple_events(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """If the model has rejected the memory three times, count_in_window
    should say 3 — the user-visible signal is "this keeps getting
    rejected", not just "it was rejected once"."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    for _ in range(3):
        await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert len(annotations) == 1
    assert annotations[0]["count_in_window"] == 3


async def test_claim_excerpt_propagates_from_t11(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """T1.1 + T2.3 integration: when the ignored event carried a
    claim_excerpt, the annotation carries it too. This is what makes
    the rejection actionable — the model sees not just "rejected" but
    "*this specific claim* was rejected", and can rephrase.

    The body has to stay a user-shaped claim — it is the string the
    `claim_excerpts` assertion below matches verbatim, and a claim about
    the user is the realistic case for a rejection worth echoing back.
    So the seed acknowledges `UserClaimGate` rather than dodging it by
    rewording."""
    server, _ = server_with_rec
    mid = await _seed(
        server, "the user prefers terse explanations", acknowledge_user_claim=True
    )

    await _call(
        server,
        "memory_record_use",
        memory_ids=[mid],
        outcome="ignored",
        claim_excerpts=["the user prefers terse explanations"],
    )

    hits = _unwrap(await _call(server, "memory_search", query="terse"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    assert annotations[0]["claim_excerpt"] == "the user prefers terse explanations"


async def test_other_hit_unannotated_when_no_negatives(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """The annotation must not leak across hits. A search returning two
    memories where only one has negative events should annotate only
    the affected hit."""
    server, _ = server_with_rec
    a_id = await _seed(server, "python list comprehension")
    b_id = await _seed(server, "python decorators and closures")

    await _call(server, "memory_record_use", memory_ids=[a_id], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    by_id = {h["id"]: h for h in hits}
    assert "recent_negative_outcomes" in by_id[a_id]
    assert "recent_negative_outcomes" not in by_id[b_id]


async def test_each_outcome_at_most_one_entry(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Even with many ignored events, the annotation has one entry per
    outcome type — the count_in_window captures multiplicity. Keeps
    the wire shape compact."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")
    for _ in range(5):
        await _call(server, "memory_record_use", memory_ids=[mid], outcome="ignored")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    ignored_entries = [a for a in annotations if a["outcome"] == "ignored"]
    assert len(ignored_entries) == 1


async def test_most_recent_ts_is_latest_event_timestamp(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """When multiple ignored events stack, `most_recent_ts` must be the
    latest one — the caller uses this to decide "is this rejection
    still fresh enough to matter?"."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension")

    # Three ignored events in sequence; the third is the "most recent".
    timestamps: list[str] = []
    for _ in range(3):
        res = await _call(
            server, "memory_record_use", memory_ids=[mid], outcome="ignored"
        )
        # The record_use call doesn't return ts; we rely on iteration
        # order being chronological.
        timestamps.append(res.get("outcome", ""))

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    annotations = hits[0].get("recent_negative_outcomes")
    assert annotations is not None
    # The most_recent_ts must be a parsable, ISO-formatted string.
    most_recent = annotations[0]["most_recent_ts"]
    assert most_recent.endswith("Z") or "+" in most_recent


def _append_raw_event(memory_dir: Path, event: dict[str, Any]) -> None:
    """Append one raw JSON event line to the active event log.

    Bypasses the `Recorder` (which only ever stamps a canonical UTC
    `…Z` ts) so a test can pin a specific on-disk `ts` shape — here, an
    offset-less timestamp the way a hand-edited or legacy event carries
    it. `iter_events` reads exactly this file and tolerates extra lines.
    """
    path = memory_dir / EVENT_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


async def test_offsetless_negative_outcome_ts_does_not_crash_search(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """Regression for the `_parse_iso_ts` naive-datetime bug (FIX 2).

    `attach_recent_negative_outcomes` parsed each `use` event's `ts`
    with a bespoke `_parse_iso_ts` that only handled the canonical
    `…Z` shape — an OFFSET-LESS ts (e.g. a hand-written or legacy
    `"2026-05-31T12:00:00"`) came back NAIVE. When the same memory has
    BOTH an offset-less and a canonical-`…Z` negative event, the per-id
    timeline then mixed naive and tz-aware datetimes, and
    `timeline.sort(key=lambda e: e["ts"])` raised
    `TypeError: can't compare offset-naive and offset-aware datetimes`,
    aborting an otherwise-successful `memory_search`.

    Swapping to the canonical `parse_event_ts` stamps UTC on the
    offset-less value, so the whole timeline is tz-aware and the sort
    is well-typed. This seeds exactly that mixed pair and asserts the
    search returns the hit with both events counted.
    """
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension notes")

    memory_dir = Path(server_with_rec[1].root)
    now = datetime.now(timezone.utc)
    # Canonical `…Z` ts (tz-aware once parsed) — the shape the recorder
    # always writes.
    canonical_ts = (now.replace(microsecond=0)).isoformat().replace("+00:00", "Z")
    # Offset-less ts (naive under the old parser) — a few minutes older,
    # still well inside the 30-day window. This is the value that used to
    # poison the sort.
    offsetless_ts = now.replace(microsecond=0, tzinfo=None).isoformat()
    assert "Z" not in offsetless_ts and "+" not in offsetless_ts

    for ts in (canonical_ts, offsetless_ts):
        _append_raw_event(
            memory_dir,
            {
                "ts": ts,
                "session": "sess_legacy",
                "kind": "use",
                "ids": [mid],
                "outcome": "ignored",
            },
        )

    # On the unfixed code this raises TypeError inside the timeline sort.
    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits, "search should still return the hit, not abort on the sort"
    by_id = {h["id"]: h for h in hits}
    annotations = by_id[mid].get("recent_negative_outcomes")
    assert annotations is not None, (
        "both negative events should surface — the offset-less ts must "
        "not crash the annotation pass"
    )
    ignored = [a for a in annotations if a["outcome"] == "ignored"]
    assert len(ignored) == 1
    # Both events landed in the same window and were counted.
    assert ignored[0]["count_in_window"] == 2


def test_offsetless_negative_outcome_ts_windowed_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the windowing half of FIX 2.

    Beyond crashing the sort, the old naive parse made an offset-less
    event's `ts.timestamp()` read in the HOST's local zone — silently
    shifting the event by the local UTC offset and pushing it out of (or
    into) the 30-day window depending on where the box happens to live.
    `parse_event_ts` stamps the offset-less value as UTC so the window
    math is correct everywhere.

    Driven directly against `ResponseBuilder.attach_recent_negative_outcomes`
    so `now` is injectable and the assertion is deterministic. Mirrors the
    hook-side pin in `test_hook.py`: force `Asia/Kolkata` (UTC+05:30, no
    DST), pin `now`, and place a lone offset-less event 2h INSIDE the UTC
    cutoff. Under the buggy local parse that same wall-clock reads 5h30m
    earlier in absolute UTC — i.e. *before* the cutoff — so it would be
    dropped from the window and the annotation would vanish. Under the fix
    it's UTC and stays in-window. A single event means the sort can't
    crash, isolating the windowing behaviour.
    """
    import sys
    import time

    if sys.platform == "win32":
        pytest.skip("time.tzset() / TZ override is POSIX-only")

    monkeypatch.setenv("TZ", "Asia/Kolkata")  # UTC+05:30, no DST
    time.tzset()
    try:
        now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
        # 30-day window → cutoff = 2026-05-01T12:00:00Z. Offset-less event
        # at naive 2026-05-01T14:00:00 is 2h AFTER the cutoff in UTC, but
        # under the +05:30 local parse it resolves to 2026-05-01T08:30:00Z
        # — 3h30m BEFORE the cutoff, i.e. dropped.
        offsetless_ts = "2026-05-01T14:00:00"
        hit = MemoryHit(
            id="mem_offsetless",
            scopes=["tools"],
            confidence=Confidence.MEDIUM,
            snippet="…",
            score=1.0,
            relevance="high",
            created=now,
            updated=now,
        )
        events = [
            {
                "ts": offsetless_ts,
                "session": "sess_legacy",
                "kind": "use",
                "ids": ["mem_offsetless"],
                "outcome": "ignored",
            }
        ]
        out: list[dict[str, Any]] = [{"id": "mem_offsetless"}]
        builder = ResponseBuilder(stale_after_days=30)
        builder.attach_recent_negative_outcomes(
            out, [hit], events, now=now, window_days=30
        )
        annotations = out[0].get("recent_negative_outcomes")
        assert annotations is not None, (
            "an offset-less event 2h inside the UTC cutoff must be windowed "
            "as UTC and surface; under the local-time parse it falls 3h30m "
            "before the cutoff and is silently dropped"
        )
        assert annotations[0]["outcome"] == "ignored"
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


async def test_poison_id_shapes_do_not_crash_search_and_claims_stay_aligned(
    server_with_rec: tuple[Any, Recorder],
) -> None:
    """One malformed `use` event in the plaintext log was a FULL retrieval
    outage until rotation: `attach_recent_negative_outcomes` runs on every
    hit-producing memory_search with NO flag gate and iterated `ids` raw —
    `"ids": 42` raises TypeError, `[[id]]` is unhashable at the hit-set
    lookup. These are the poison shapes 3.15.0 hardened memory_health
    against while leaving this walk raw. The shared normalizer must (a)
    survive the poison and (b) preserve each id's ORIGINAL index, because
    `claim_excerpts` is recorded parallel to the raw list — compacting
    dropped elements would silently shift every later claim onto the wrong
    memory. Reverting to the raw iteration crashes the search; compacting
    indices mis-attributes the claim; both fail below."""
    server, _ = server_with_rec
    mid = await _seed(server, "python list comprehension notes")
    memory_dir = Path(server_with_rec[1].root)
    now = datetime.now(timezone.utc)
    ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for poison_ids in (42, [["m-nested"]], {"d": 1}):
        _append_raw_event(
            memory_dir,
            {
                "ts": ts,
                "session": "sess_poison",
                "kind": "use",
                "ids": poison_ids,
                "outcome": "ignored",
            },
        )
    # Alignment probe: the real id sits at ORIGINAL index 2, BEHIND a
    # malformed element; its parallel claim excerpt must stay attached.
    _append_raw_event(
        memory_dir,
        {
            "ts": ts,
            "session": "sess_poison",
            "kind": "use",
            "ids": ["m-gone", ["malformed"], mid],
            "outcome": "contradicted",
            "claim_excerpts": [
                "claim-for-m-gone",
                "claim-for-malformed",
                "the right claim",
            ],
        },
    )

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits, "poison events must not abort the search"
    by_id = {h["id"]: h for h in hits}
    annotations = by_id[mid].get("recent_negative_outcomes")
    assert annotations, "the well-formed contradicted event must still surface"
    contradicted = [a for a in annotations if a["outcome"] == "contradicted"]
    assert contradicted, "contradicted outcome should be annotated"
    assert contradicted[0].get("claim_excerpt") == "the right claim", (
        "claim_excerpts is parallel to the RAW ids list; dropping the "
        "malformed element must not shift the surviving id onto a "
        "neighbor's claim"
    )
