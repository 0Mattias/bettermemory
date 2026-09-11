"""A schema bump must not tell the user their healthy store is corrupt.

Releasing a schema bump does not only migrate the index; it strands every
process still running the previous version. The upgraded CLI or hook opens
the store and migrates the index, and the long-lived MCP servers — which
keep the code they imported until their client restarts — are suddenly
readers too old for the file in front of them. `index.status()` folded that
state into the same `corrupt=True` shape as a torn file, so a store with
nothing wrong with it reported as damaged on every surface, and every
surface prescribed `bettermemory reindex`, which cannot resolve a version
skew in either direction: run by the older binary it refuses for the same
reason, run by the newer one it rewrites the index at a schema the older
reader still cannot read. The only fix is restarting the client.

Measured on the owner's own store while dogfooding the 7.12.0 cut
(schema 10 to 11), which is what this file pins. Two claims run through
every test here: version skew is NOT corruption, and could-not-ask never
manufactures a verdict — the third state is published rather than folded
into one of the two that already existed.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory import doctor, index
from bettermemory import store as store_module
from bettermemory._handlers import load_search_candidates
from bettermemory._response import (
    TRUST_UNAVAILABLE_RECOMMENDATION,
    TRUST_UNAVAILABLE_SCHEMA_SKEW_RECOMMENDATION,
)
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server, main as cli_main
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call


def _skew(memory_dir: Path, *, ahead: int = 1) -> int:
    """Make the on-disk index one schema version NEWER than this reader.

    The honest reproduction of the real thing: the store and its .md
    files are never touched, only the index's recorded schema version,
    exactly as an upgraded sibling process would leave it behind.
    """
    target = index.SCHEMA_VERSION + ahead
    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(target),)
        )
        conn.commit()
    finally:
        conn.close()
    return target


def _seed(memory_dir: Path, n: int = 3) -> Store:
    store = Store(memory_dir)
    for i in range(n):
        store.write(
            content=f"the auth service listens on port 844{i}", scopes=["tools"]
        )
    index.rebuild(memory_dir, store.iter_active())
    return store


def test_a_healthy_store_read_by_an_old_reader_is_not_corrupt(
    memory_dir: Path,
) -> None:
    """G1, with its premise asserted rather than assumed.

    The defect is not "an unreadable index reports badly" — it is that a
    store with NOTHING wrong with it reports as damaged. So the test has
    to establish health on both sides of the poison: a clean status with
    a real count before, and the memories still readable from disk after.
    Without that half this is indistinguishable from a corruption test.
    """
    store = _seed(memory_dir)

    before = index.status(memory_dir)
    assert before["indexed_count"] == 3, "premise: the index really was populated"
    assert "corrupt" not in before and "schema_skew" not in before
    assert index.index_unreadable(before) is False

    target = _skew(memory_dir)
    after = index.status(memory_dir)

    assert after["schema_skew"] is True
    assert "corrupt" not in after, (
        "a version-skewed index is intact; calling it corrupt is the defect"
    )
    assert after["schema_version"] == target, "the on-disk schema IS knowable"
    assert after["reader_schema_version"] == index.SCHEMA_VERSION
    assert after["exists"] is True
    assert index.index_unreadable(after) is True

    # The other half of the premise: the store itself is untouched. This
    # is what makes "corrupt" a lie rather than a wording quibble.
    assert len(store.load_all()) == 3, "the memories were never in danger"
    assert len(list(memory_dir.glob("*.md"))) == 3


def test_skew_and_corruption_never_collapse_into_each_other(
    memory_dir: Path, tmp_path: Path
) -> None:
    """G2, in BOTH directions. Splitting a state is only worth anything
    if the split holds: skew must never set `corrupt`, and genuine
    damage must never claim to be a mere version skew and send the user
    to a restart that cannot repair a torn file."""
    _seed(memory_dir)
    _skew(memory_dir)
    skewed = index.status(memory_dir)
    assert skewed.get("schema_skew") is True and "corrupt" not in skewed

    # Real damage: a file that is not a database at all.
    torn_dir = tmp_path / "torn"
    torn_dir.mkdir()
    _seed(torn_dir)
    index.index_path(torn_dir).write_bytes(b"not a sqlite database at all " * 16)
    torn = index.status(torn_dir)
    assert torn.get("corrupt") is True
    assert "schema_skew" not in torn, "a torn file is not a version skew"
    assert index.index_unreadable(torn) is True
    assert index.unreadable_remedy(torn) == index.INDEX_CORRUPT_REMEDY
    assert index.unreadable_remedy(skewed) == index.SCHEMA_SKEW_REMEDY

    # And the remedies are not interchangeable prose. The skew remedy
    # does name `reindex` — but only to rule it out, which is worth
    # saying to a user who has just been told to run it everywhere
    # else. So the assertion is about ORDER and negation, not absence:
    # the restart is the instruction, reindex is the thing that cannot
    # help.
    skew = index.SCHEMA_SKEW_REMEDY.lower()
    assert skew.index("restart") < skew.index("reindex"), (
        "the skew remedy must lead with the action that works"
    )
    assert "cannot" in skew, "reindex may only appear here as a negation"
    assert "reindex" in index.INDEX_CORRUPT_REMEDY.lower()
    assert "restart" not in index.INDEX_CORRUPT_REMEDY.lower()


def test_every_router_still_falls_back_under_skew(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3a — the search router does not use an index it cannot read.

    Premise asserted rather than assumed: "it fell back" is worthless on
    its own, because a store under the index threshold always falls
    back. So the same call is made twice, and the first one must take
    the index path.

    Worth being exact about what this does NOT prove. Reverting this
    router to `status.get("corrupt")` alone leaves the test GREEN: the
    skew shape carries no `indexed_count`, so the fallthrough lands on
    `.get("indexed_count", 0)` -> 0, under every legal threshold, and
    the threshold arm rescues the routing by accident. Here the factored
    predicate is defensive rather than load-bearing. The surface where
    the fallthrough really does publish a wrong answer is session-start,
    and it has its own test below.
    """
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    store = _seed(memory_dir)

    _, _, prefiltered = load_search_candidates(store, "auth service")
    assert prefiltered is True, "premise: this query really does use the index"

    _skew(memory_dir)

    candidates, saturated, prefiltered = load_search_candidates(store, "auth service")
    assert prefiltered is False, "an index this reader cannot read must not be used"
    assert saturated is False
    assert len(candidates) == 3, "and the fallback still serves the whole corpus"


def test_session_start_never_invents_a_count_divergence_under_skew(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """G3b — the fallthrough that is NOT rescued by anything.

    Unlike the search router, session-start's next gate compares
    `indexed_count` against the disk count. A version-skewed status
    carries no count, so a gate still testing `corrupt` alone reads 0
    rows against N files and prints a divergence that does not exist —
    inventing a disagreement between an index that is fine and a disk
    that is fine, and prescribing `reindex` to reconcile them. That is a
    manufactured verdict from a could-not-ask, which is the exact thing
    the 7.9.0 doctrine forbids.
    """
    _seed(memory_dir)
    _skew(memory_dir)

    monkeypatch.setattr(sys, "argv", ["bettermemory", "session-start"])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    with pytest.raises(SystemExit):
        cli_main()

    err = capsys.readouterr().err
    assert "index holds" not in err, (
        "session-start invented a count divergence from a status that "
        "never published a count"
    )
    assert "0 row(s)" not in err
    assert "reconciles them" not in err


def test_doctor_names_the_restart_and_never_says_corrupt(
    memory_dir: Path, tmp_path: Path
) -> None:
    """G4. doctor is the surface an operator reaches for when something
    says "corrupt", so it is the one that most has to tell the two
    states apart — and under skew it must not print the word at all."""
    _seed(memory_dir)
    _skew(memory_dir)

    diag = doctor._check_index_health(memory_dir)

    assert diag.status == "warn"
    assert "corrupt" not in diag.message.lower(), (
        "doctor told the user their intact store was corrupt"
    )
    assert diag.fix_hint == index.SCHEMA_SKEW_REMEDY
    hint = (diag.fix_hint or "").lower()
    assert hint.index("restart") < hint.index("reindex"), (
        "doctor's fix must lead with the restart, not the repair that "
        "cannot clear this state"
    )
    assert str(index.SCHEMA_VERSION) in diag.message
    assert diag.details["schema_skew"] is True

    # Genuine corruption keeps the old answer, word for word.
    torn_dir = tmp_path / "torn"
    torn_dir.mkdir()
    _seed(torn_dir)
    index.index_path(torn_dir).write_bytes(b"not a sqlite database at all " * 16)

    torn_diag = doctor._check_index_health(torn_dir)

    assert torn_diag.status == "warn"
    assert "corrupt" in torn_diag.message.lower()
    assert torn_diag.fix_hint == index.INDEX_CORRUPT_REMEDY


def test_the_store_startup_warning_tells_the_user_to_restart(
    memory_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """G4b. The divergence warning fires at every Store construction, so
    it was the loudest way this defect reached a user: one schema bump
    and every subsequent server start announced a corrupt store."""
    _seed(memory_dir)
    _skew(memory_dir)
    store_module._DIVERGENCE_WARNED_ROOTS.discard(memory_dir)

    with caplog.at_level(logging.WARNING, logger="bettermemory.store"):
        store_module._warn_on_index_divergence(memory_dir)

    assert caplog.records, "the state is worth one warning"
    message = caplog.records[0].getMessage()
    assert "corrupt" not in message.lower()
    assert "restart" in message.lower()
    assert str(index.SCHEMA_VERSION) in message


def _build(memory_dir: Path) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=recorder
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_the_read_surfaces_carry_the_restart_recommendation(
    memory_dir: Path,
) -> None:
    """G5. The trust rule still stands down — `verified_locally_at` lives
    only in the index and an unreadable index cannot supply it, whatever
    the cause. What changes is the remedy the demoted row carries: under
    skew, sending the reader to `reindex` is advice that cannot work."""
    store = Store(memory_dir)
    memory = store.write(
        content="the auth service listens on port 8443 behind the gateway",
        scopes=["tools"],
    )
    store.mark_verified(memory.id)
    index.rebuild(memory_dir, store.iter_active())
    server = _build(memory_dir)

    hit = next(
        r
        for r in await _call(server, "memory_search", query="auth service port")
        if r["id"] == memory.id
    )
    assert "trust_unavailable" not in hit, "premise: the index was readable"
    assert hit["staleness_verdict"] == "fresh"

    _skew(memory_dir)

    hit = next(
        r
        for r in await _call(server, "memory_search", query="auth service port")
        if r["id"] == memory.id
    )
    assert hit["trust_unavailable"] is True, "the rule still cannot run"
    assert hit["staleness_verdict"] == "spot_check_required"
    assert (
        hit["verification"]["recommendation"]
        == TRUST_UNAVAILABLE_SCHEMA_SKEW_RECOMMENDATION
    )
    assert hit["verification"]["recommendation"] != TRUST_UNAVAILABLE_RECOMMENDATION

    shown = await _call(server, "memory_show", id=memory.id)
    assert shown["trust_unavailable"] is True
    assert (
        shown["verification"]["recommendation"]
        == TRUST_UNAVAILABLE_SCHEMA_SKEW_RECOMMENDATION
    )


def test_the_session_start_hint_does_not_prescribe_reindex_under_skew(
    memory_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """G6. The hint degrades to silence either way — that part is not in
    question. The defect is the line it prints on the way out, which sent
    a user whose store was fine to a repair command for damaged data."""
    _seed(memory_dir)
    _skew(memory_dir)

    monkeypatch.setattr(sys, "argv", ["bettermemory", "session-start"])
    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0, "session-start always exits 0"

    err = capsys.readouterr().err
    assert "index unusable" in err
    assert index.SCHEMA_SKEW_REMEDY in err
    assert "`bettermemory reindex` rebuilds it." not in err
