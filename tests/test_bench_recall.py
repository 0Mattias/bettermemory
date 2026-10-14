"""The recall surface's bench (unit RS, D20): bench/recall/replay.py,
bench/recall/brief.py and the prompt arm of bench/daemon/latency.py.

The replay is graded on a private fixture frozen from the sealed copy
(RS-P4); these tests pin what makes that count readable again and safe to
take: the freeze reads its store read-only and writes no body, summary or
prompt into the fixture; each rule changes one thing from M4's to the
built one (the as-of eligibility, the built project gate, the built
segment gate and renderer), so a count that moves names the step that
moved it; the cooldown never shows an id twice in a segment and stops at
twelve; the block is the built renderer's, marks included, under its cap.

No test reads the sealed copy or the owner's store; every store and
fixture here is synthetic.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from bettermemory.origin import Origin
from bettermemory.recall import RECALL_CAP_CHARS, SEGMENT_DELIVERY_CAP, HookResult

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


replay = _load("bench_recall_replay", _ROOT / "bench" / "recall" / "replay.py")
brief = _load("bench_recall_brief", _ROOT / "bench" / "recall" / "brief.py")
latency = _load("bench_daemon_latency", _ROOT / "bench" / "daemon" / "latency.py")

REPO = "https://example.invalid/acme/widgets.git"
OTHER = "https://example.invalid/acme/gadgets.git"
WT = "/nonexistent/acme/widgets"
A = "01JRECALLAAAAAAAAAAAAAAAA1"
B = "01JRECALLAAAAAAAAAAAAAAAA2"
C = "01JRECALLAAAAAAAAAAAAAAAA3"


def _ts(minute: int) -> str:
    return f"2026-09-01T10:{minute:02d}:00.000000Z"


def _memory(**over: Any) -> dict[str, Any]:
    row = {
        "created": "2026-08-01T00:00:00.000000Z",
        "category": "fact",
        "repo": REPO,
        "worktree_root": WT,
        "provenance": "local",
        "summary_chars": 80,
    }
    row.update(over)
    return row


def _turn(minute: int, hits: list[str], **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": _ts(minute),
        "session": "sess_one",
        "worktree_root": WT,
        "verdict": "ok",
        "hits": [[hit, "high"] for hit in hits],
    }
    row.update(over)
    return row


def _fixture(
    turns: list[dict[str, Any]],
    memories: dict[str, dict[str, Any]] | None = None,
    **over: Any,
) -> dict[str, Any]:
    mems = (
        memories if memories is not None else {A: _memory(), B: _memory(), C: _memory()}
    )
    fixture: dict[str, Any] = {
        "kind": replay.FIXTURE_KIND,
        "version": replay.FIXTURE_VERSION,
        "window": {"since": "2026-09-01", "until": "2026-09-02"},
        "store_sha256": "0" * 64,
        "turns": turns,
        "callers": {WT: {"repo": REPO, "source": "memories"}},
        "project": {WT: sorted(m for m, row in mems.items() if row["repo"] == REPO)},
        "memories": mems,
        "contradicted": {},
        "answered": {},
        "contradicted_at_freeze": [],
        "conflicts": [],
        "events": [],
    }
    fixture.update(over)
    return fixture


def _deliveries(fixture: dict[str, Any], rule: str) -> int:
    return int(replay.replay(fixture, rule)["counts"].get("deliveries", 0))


# ---------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------


def _store(path: Path, worktree: Path) -> Path:
    """A minimal v9-shaped store: the columns the freeze reads."""
    db = path / "memory.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE memories (id TEXT, created TEXT, updated TEXT, category TEXT,"
        " origin_repo TEXT, origin_worktree TEXT, provenance TEXT, body TEXT);"
        "CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT, session TEXT,"
        " kind TEXT, payload TEXT);"
        "CREATE TABLE conflicts (id TEXT, a_id TEXT, b_id TEXT, created TEXT,"
        " status TEXT, verdict_ts TEXT);"
    )
    body = "SECRET-BODY-TEXT " * 12
    for memory_id, repo in ((A, REPO), (B, OTHER)):
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)",
            (memory_id, _ts(0), _ts(0), "fact", repo, str(worktree), "local", body),
        )
    turn = {
        "probe_query": {"len": 13, "preview": "SECRET-PROMPT"},
        "top_hits": [
            {"id": A, "relevance": "high", "score": 1.0},
            {"id": B, "relevance": "medium", "score": 0.5},
        ],
        "verdict": "ok",
        "worktree_root": str(worktree),
        "session_id": "sess_one",
    }
    rows = [
        (_ts(5), "sess_one", "turn_audited", turn),
        ("2026-08-01T00:00:00.000000Z", "sess_one", "turn_audited", turn),
        (_ts(3), "sess_one", "search", {"returned": [A], "query": "SECRET-QUERY"}),
        (_ts(4), "sess_two", "use", {"ids": [A], "outcome": "contradicted"}),
    ]
    for ts, session, kind, payload in rows:
        conn.execute(
            "INSERT INTO log (ts, session, kind, payload) VALUES (?,?,?,?)",
            (ts, session, kind, json.dumps(payload)),
        )
    conn.commit()
    conn.close()
    return db


def test_freeze_reads_read_only_and_writes_no_text(tmp_path: Path) -> None:
    worktree = tmp_path / "widgets"
    worktree.mkdir()
    db = _store(tmp_path, worktree)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    fixture = replay.freeze(
        db, since="2026-09-01", until="2026-09-02", git_remotes=False
    )
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    dumped = json.dumps(fixture)
    for secret in ("SECRET-BODY-TEXT", "SECRET-PROMPT", "SECRET-QUERY"):
        assert secret not in dumped
    # Only the window's turn, its hits as (id, relevance).
    assert [t["ts"] for t in fixture["turns"]] == [_ts(5)]
    assert fixture["turns"][0]["hits"] == [[A, "high"], [B, "medium"]]
    # The caller map is frozen from the memories, the project map from
    # the built `in_project` on it.
    # (A tie goes to the repository met first.)
    assert fixture["callers"] == {str(worktree): {"repo": REPO, "source": "memories"}}
    assert fixture["project"][str(worktree)] == [A]
    assert fixture["memories"][A]["summary_chars"] == 80
    assert fixture["contradicted"] == {A: [_ts(4)]}
    assert fixture["contradicted_at_freeze"] == [A]
    # The session's own rows and the checkout's are kept; another
    # session's row stamped with no worktree is not.
    assert [e["kind"] for e in fixture["events"]] == ["search"]


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def test_m4_rule_reproduces_the_session_touch_gate() -> None:
    """M4 held an id back once a verify (or show, write, update, use)
    under the session came before the turn; the built gate reads
    retrievals, writes and updates only, so the verify does not hold it,
    and the update holds it under both."""
    verify = {"ts": _ts(1), "kind": "verify", "session": "sess_one", "id": A}
    fixture = _fixture([_turn(2, [A])], events=[verify])
    assert _deliveries(fixture, "m4") == 0
    assert _deliveries(fixture, "rs-v1") == 1
    update = {"ts": _ts(1), "kind": "update", "session": "sess_one", "id": A}
    fixture = _fixture([_turn(2, [A])], events=[update])
    assert _deliveries(fixture, "m4") == 0
    assert _deliveries(fixture, "rs-v1") == 0


def test_built_gate_holds_a_retrieved_hit_and_the_worktree_half() -> None:
    own = {"ts": _ts(1), "kind": "search", "session": "sess_one", "returned": [A]}
    assert _deliveries(_fixture([_turn(2, [A])], events=[own]), "m4") == 1
    assert _deliveries(_fixture([_turn(2, [A])], events=[own]), "rs-v1") == 0
    # Another session's retrieval in the same checkout after this
    # session's segment opened counts; one before it does not.
    later = {
        "ts": _ts(3),
        "kind": "show",
        "session": "sess_two",
        "worktree_root": WT,
        "id": A,
    }
    turns = [_turn(2, [B]), _turn(4, [A])]
    assert _deliveries(_fixture(turns, events=[later]), "rs-v1") == 1
    earlier = dict(later, ts="2026-09-01T09:00:00.000000Z")
    assert _deliveries(_fixture(turns, events=[earlier]), "rs-v1") == 2


def test_asof_reads_a_contradiction_answered_before_the_turn() -> None:
    """Contradicted, then updated, before the turn: open at the window's
    end (a later contradiction) but not at the turn."""
    fixture = _fixture(
        [_turn(5, [A])],
        contradicted={A: [_ts(1), _ts(9)]},
        answered={A: [_ts(2)]},
        contradicted_at_freeze=[A],
    )
    assert _deliveries(fixture, "m4") == 0
    assert _deliveries(fixture, "asof") == 1
    still_open = dict(fixture, answered={})
    assert _deliveries(still_open, "asof") == 0


def test_asof_holds_a_memory_written_after_the_turn() -> None:
    fixture = _fixture([_turn(5, [A])], {A: _memory(created=_ts(7))})
    assert _deliveries(fixture, "m4") == 1
    assert _deliveries(fixture, "asof") == 0


def test_project_step_applies_the_built_gate_and_drops_the_ambient_hold() -> None:
    fixture = _fixture(
        [_turn(5, [A, B])],
        {A: _memory(category="ambient"), B: _memory()},
        project={WT: [B]},
    )
    assert replay.replay(fixture, "asof")["candidates_histogram"] == {1: 1}
    held = replay.replay(fixture, "project")["held_back"]
    assert held == {"other_project": 1}


def test_cooldown_shows_no_id_twice_and_stops_at_twelve() -> None:
    ids = [f"01JRECALLBBBBBBBBBBBBBBB{index:02d}" for index in range(14)]
    memories = {memory_id: _memory() for memory_id in ids}
    turns = [_turn(index, [memory_id]) for index, memory_id in enumerate(ids)]
    turns.append(_turn(30, [ids[0]]))
    for rule in replay.RULES:
        read = replay.replay(_fixture(turns, memories), rule)
        assert read["counts"]["deliveries"] == SEGMENT_DELIVERY_CAP
        assert read["counts"]["segment_cap"] == 2
        assert read["ids_shown_twice_in_a_segment"] == 0
        assert read["deliveries_per_session_max"] == SEGMENT_DELIVERY_CAP


def test_block_is_the_built_renderer_with_marks_under_its_cap() -> None:
    memories = {m: _memory() for m in (A, B, C)}
    pending = {
        "id": "cf-0123456789abcdef",
        "a_id": A,
        "b_id": "01JRECALLZZZZZZZZZZZZZZZZZ",
        "created": _ts(1),
        "status": "pending",
        "verdict_ts": None,
    }
    read = replay.replay(
        _fixture([_turn(5, [A, B, C])], memories, conflicts=[pending]), "rs-v1"
    )
    assert read["counts"]["deliveries_with_mark"] == 1
    assert read["block_chars_max"] <= RECALL_CAP_CHARS
    unmarked = replay.replay(_fixture([_turn(5, [A, B, C])], memories), "rs-v1")
    assert read["block_chars_max"] > unmarked["block_chars_max"]
    # A pair raised after the turn, or decided before it, marks nothing.
    after = dict(pending, created=_ts(9))
    decided = dict(pending, status="dismissed", verdict_ts=_ts(2))
    for row in (after, decided):
        late = replay.replay(
            _fixture([_turn(5, [A, B, C])], memories, conflicts=[row]), "rs-v1"
        )
        assert late["counts"]["deliveries_with_mark"] == 0
    # Decided after the turn: pending at it.
    reopened = dict(pending, status="dismissed", verdict_ts=_ts(8))
    marked = replay.replay(
        _fixture([_turn(5, [A, B, C])], memories, conflicts=[reopened]), "rs-v1"
    )
    assert marked["counts"]["deliveries_with_mark"] == 1


def test_no_signal_and_outside_a_repository_never_deliver() -> None:
    turns = [
        _turn(1, [A], verdict="no_signal"),
        _turn(2, [B], worktree_root=None),
        dict(_turn(3, [C]), hits=[[C, "low"]]),
    ]
    read = replay.replay(_fixture(turns), "rs-v1")
    assert read["counts"] == {
        "in_repository": 2,
        "no_candidate": 1,
        "no_signal": 1,
        "outside_repository": 1,
        "turns": 3,
    }
    assert read["held_back"] == {"low_relevance": 1}
    assert read["fire_rate_in_repository"] == 0.0


def test_unknown_rule_is_refused() -> None:
    with pytest.raises(ValueError):
        replay.replay(_fixture([]), "m5")


def test_run_writes_counts_rates_and_the_fixture_sha(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    replay.write_json(path, _fixture([_turn(1, [A]), _turn(2, [B])]))
    out = tmp_path / "result.json"
    assert replay.main(["run", "--fixture", str(path), "--out", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["kind"] == replay.RESULT_KIND
    assert result["fixture"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert set(result["rules"]) == set(replay.RULES)
    assert all(read["counts"]["deliveries"] == 2 for read in result["rules"].values())
    with pytest.raises(SystemExit):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"kind": "other"}))
        replay.load_fixture(bad)


# ---------------------------------------------------------------------------
# brief.py
# ---------------------------------------------------------------------------


def test_count_git_counts_git_processes_only() -> None:
    with brief.count_git() as spawned:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        assert spawned() == 1
    assert subprocess.run.__name__ == "run"


@pytest.mark.skipif(sys.platform == "win32", reason="the git shim is a shell script")
def test_git_shim_logs_each_call_and_runs_git(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    shim = brief.git_shim(tmp_path / "shim", log)
    proc = subprocess.run(
        [str(shim / "git"), "--version"], capture_output=True, text=True, check=True
    )
    assert proc.stdout.startswith("git version")
    subprocess.run([str(shim / "git"), "--version"], capture_output=True, check=True)
    assert brief._lines(log) == 2


def test_checkouts_rank_repositories_and_name_none() -> None:
    def mem(repo: str | None, worktree: str | None) -> Any:
        return SimpleNamespace(origin=Origin(repo=repo, worktree_root=worktree))

    store = SimpleNamespace(
        load_all=lambda: [
            mem(REPO, WT),
            mem(REPO, WT),
            mem(REPO, "/nonexistent/other"),
            mem(None, "/nonexistent/repoless"),
            mem(None, "/nonexistent/repoless"),
            mem(OTHER, None),
            SimpleNamespace(origin=None),
        ]
    )
    rows = brief.checkouts(store, count=2)
    assert [(r["rank"], r["rows"], r["repoless"]) for r in rows] == [
        (0, 3, False),
        (1, 2, True),
    ]
    assert rows[0]["local"] is None and rows[0]["origin"].repo == REPO
    public = brief._public(rows[1])
    assert set(public) == {
        "rank",
        "rows",
        "is_this_repository",
        "repoless",
        "local_worktree",
    }
    assert "/nonexistent" not in json.dumps(public)


def test_line_faults_flags_a_line_beyond_id_summary_and_mark() -> None:
    body = "A plain durable note about the widget build. " * 4
    memory = SimpleNamespace(id=A, body=body)
    store = SimpleNamespace(
        pending_conflict_pairs=lambda: [],
        load_many=lambda ids: [memory],
    )
    from bettermemory.recall import render_brief

    text, ids = render_brief([memory])  # type: ignore[list-item]
    good = HookResult(stdout=text, surface="session_brief", ids=tuple(ids))
    assert brief._line_faults(store, good) == 0
    bad = HookResult(stdout=text + " extra", surface="session_brief", ids=tuple(ids))
    assert brief._line_faults(store, bad) == 1
    longer = HookResult(
        stdout=text + "\n- more", surface="session_brief", ids=tuple(ids)
    )
    assert brief._line_faults(store, longer) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the signature reads the POSIX stamps of the repository files",
)
def test_signature_sees_a_root_file_created_and_removed(tmp_path: Path) -> None:
    """The held stamps are part of the signature: a file created and
    removed in the root leaves the head's signature and the walk files as
    they were, and moves the signature all the same, so a fill that lost
    its memo to it is visible in the results."""
    from .test_verdict import _repo

    repo = _repo(tmp_path)
    before = brief._signature(str(repo))
    assert before is not None and before[1] is not None and before[2]
    assert brief._signature(str(repo)) == before
    scratch = repo / ".notes.md.swp"
    scratch.write_text("x")
    scratch.unlink()
    after = brief._signature(str(repo))
    assert after[:2] == before[:2]
    assert after != before
    assert brief._signature(None) is None


def test_fill_counts_read_the_checkouts_last_fill() -> None:
    from bettermemory.recall import brief_key
    from bettermemory.verdict import FillReport

    origin = Origin(repo=REPO, worktree_root=WT, cwd=None)
    report = FillReport(rows=5, resolved=4, kept=3, retried=1, lost=1)
    state = SimpleNamespace(
        fill_report=lambda key: report if key == brief_key(origin) else None
    )
    checkout = {"local": None, "origin": origin}
    assert brief._fill_counts(state, checkout) == {
        "fill_rows": 5,
        "fill_resolved": 4,
        "fill_kept": 3,
        "fill_retried": 1,
        "fill_lost": 1,
    }
    nothing = SimpleNamespace(fill_report=lambda key: None)
    assert set(brief._fill_counts(nothing, checkout).values()) == {None}


# ---------------------------------------------------------------------------
# latency.py's prompt arm
# ---------------------------------------------------------------------------


def test_prompt_workload_is_fixed_and_reaches_long_prompts() -> None:
    first = latency.prompt_workload(30)
    assert first == latency.prompt_workload(30)
    assert len(first) == 30
    questions = set(latency.queries(10_000))
    for index, prompt in enumerate(first):
        if index % latency.PROMPT_SHORT_EVERY == 0:
            assert prompt in questions and len(prompt) < 200
        else:
            low, high = latency.PROMPT_LONG_RANGE
            assert low <= len(prompt) <= high
    buckets = latency._length_buckets(first)
    assert buckets["under_200"] == 10 and buckets.get("1000_2000", 0) > 0


def test_prompt_deliveries_count_pointers_and_repeats() -> None:
    prompts = ["p"] * 8
    replies = [
        {"ids": [A, B]},
        {"ids": []},
        {"ids": [A]},
        {},
        {"ids": [C]},
        {"ids": []},
        {"ids": [A]},
        {"ids": []},
    ]
    read = latency.prompt_deliveries(prompts, replies, "service")
    # Prompts 0-5 are one session (A shown twice there); prompt 6 opens
    # the next.
    assert read == {
        "prompts": 8,
        "deliveries": 4,
        "pointers": 5,
        "ids_shown_twice_in_a_session": 1,
    }
