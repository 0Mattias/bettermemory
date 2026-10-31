"""RS-F7 P6's reader (bench/recall/read_rs_f7_p6.py): every standing
ruling in force reaches a standing write's chain.

Each store here is written through the product's own handlers (the
`memory_write` and `memory_update` tools on an in-process server, the
roster's harness), then copied by an online backup through a ``mode=ro``
connection, the copy the reader is run on. What the tests pin: a standing
write whose chain showed every ruling in force, on its roster or in its
related set, reads as holding its full set, read as of its commit, so a
ruling written after it is no part of it; two standing writes are read in
commit order, each at its own time; a ruling in force at the commit and
never shown is a miss naming it; a write that is not standing is not
counted, and one shown a roster before its answer dropped is counted
apart; a superseded ruling, a held one, a declared `supersedes` target and
a ruling in a disabled scope are not expected; a probe session's write and
update chain are left out; an update's roster is reported apart, read from
its own commit with the memory itself left out; a commit answering
conflict on a named ruling is M6's second clause, from the answers or,
with telemetry off, from the review's filings, and a conflict on a record
that is not standing is not; the store the product opens is refused.

Scopes, bodies and quotes are invented; no test reads the owner's store.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bettermemory import _handlers, review, standing
from bettermemory.admission import EVERY_RECORD
from bettermemory.builder import build_server
from bettermemory.events import Recorder
from bettermemory.handlers import write as write_handler
from bettermemory.handlers.standing_review import UPDATE_DOOR
from bettermemory.identity import ENV_HARNESS_SESSION, HEADER_CLIENT
from bettermemory.origin import Origin
from bettermemory.recall import WORDS
from bettermemory.session import SessionRegistry
from bettermemory.store import StandingAnswer, Store

from ._mcp import call_tool, fake_ctx
from ._review_rows import ORIGIN, FACTS, Harness, fix_origin_and_identity, is_roster
from ._review_rows import set_tau
from .test_write_roster import NEAR, OTHER_RULING, PLAN, RULING

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


p6 = _load(
    "bench_recall_read_rs_f7_p6", _ROOT / "bench" / "recall" / "read_rs_f7_p6.py"
)

QUOTE = "replace the vendor note with the new one"
VAULT = "vault"
PROBE = "switch-probe-0001"


@pytest.fixture
def checkout(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    repo = fix_origin_and_identity(monkeypatch, tmp_path_factory.mktemp("checkout"))
    set_tau(monkeypatch)
    WORDS.clear()
    yield repo
    WORDS.clear()


@pytest.fixture
def h(checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path / "store", monkeypatch)


def _standing(h: Harness, record_id: str, probability: float = 0.9) -> None:
    h.store.put_standing(record_id, StandingAnswer(probability, "asked", "test/test"))


def _ruling(h: Harness, text: str, scopes: list[str] | None = None) -> str:
    record_id = h.store.write(
        content=text, scopes=scopes or ["tools"], origin=ORIGIN, home=h.home
    ).id
    _standing(h, record_id)
    return record_id


def _replace(h: Harness, target: str, text: str) -> str:
    """A record that supersedes `target`, written straight to the store."""
    return h.store.write(
        content=text,
        scopes=["tools"],
        origin=ORIGIN,
        home=h.home,
        links=[{"type": "supersedes", "target_id": target}],
    ).id


def _seed_case(h: Harness) -> tuple[str, str, list[str]]:
    """Two rulings in force and the records nearer the plan, which hold
    its related set."""
    ruling = _ruling(h, RULING)
    other = _ruling(h, OTHER_RULING)
    near = [h.seed(text) for text in NEAR]
    return ruling, other, near


async def _to_roster(
    h: Harness, content: str = PLAN, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The review of `content` answered unrelated with its standing at
    0.9; return the answers and the roster page."""
    first = await h.write(content, **kwargs)
    sent = h.answers(first, standing=0.9)
    page = await h.write(content, review=sent, **kwargs)
    assert is_roster(page), page
    return sent, page


def _with_recorder(h: Harness, **kwargs: Any) -> None:
    """Serve the harness's next calls through a recorder built with
    `kwargs` (a worktree stamp, telemetry off)."""
    h.server = build_server(
        config=h.config,
        store=h.store,
        state=h.state,
        recorder=Recorder(store=h.store, session_id=h.state.session_id, **kwargs),
    )


def _snapshot(h: Harness, into: Path) -> Path:
    """The copy the reader is run on: an online backup of the store
    through a ``mode=ro`` connection."""
    into.mkdir(parents=True)
    copy = into / "memory.sqlite"
    source = sqlite3.connect(f"file:{h.store.path}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return copy


def _measure(h: Harness, tmp_path: Path) -> tuple[Any, Any, list[str]]:
    copy = _snapshot(h, tmp_path / "snapshot")
    now = datetime.now(timezone.utc)
    since, until = now - timedelta(hours=1), now + timedelta(hours=1)
    db = p6._ro(copy)
    try:
        found = p6.scan(db, since, until)
        replay = p6.read(db, found.commits, copy)
    finally:
        db.close()
    return found, replay, p6.render(found, replay, since, until)


def _writes(replay: Any) -> list[Any]:
    return [r for r in replay.readings if r.commit.door == p6.WRITE_DOOR]


def _rows(h: Harness, field: str) -> set[str]:
    """Every id the `review_issued` rows carry under `field`."""
    out: set[str] = set()
    for row in h.events(review.REVIEW_ISSUED):
        out.update(row.get(field) or [])
    return out


def _chain_ids(h: Harness) -> set[str]:
    """Every id a `review_issued` row showed or listed."""
    return _rows(h, "related") | _rows(h, "roster")


# ---------------------------------------------------------------------------
# HIT and MISSED
# ---------------------------------------------------------------------------


async def test_a_write_shown_every_ruling_in_force_holds_its_full_set(
    h: Harness, tmp_path: Path
) -> None:
    """Two rulings came on the roster and a third, standing, in the
    related set, which the roster leaves out: the full set is held. A
    ruling written after the commit is no part of it, since the store is
    read as it stood then."""
    ruling, other, near = _seed_case(h)
    _standing(h, near[0])
    sent, page = await _to_roster(h)
    res = await h.name(page, PLAN, sent, [])
    assert res["status"] == "committed", res
    assert near[0] in _rows(h, "related") and near[0] not in _rows(h, "roster")
    later = _ruling(h, FACTS[5])

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert reading.read and reading.commit.record == res["id"]
    assert reading.expected == {ruling, other, near[0]}
    assert later not in reading.expected
    assert reading.missing == ()
    assert "  full expected set held: 1 / 1 = 1.000" in lines
    assert "  misses: 0" in lines
    assert f"count reached {p6.TARGET_WRITES}: no (1 read standing writes)" in lines


async def test_two_standing_writes_are_read_in_commit_order_each_at_its_time(
    h: Harness, tmp_path: Path
) -> None:
    """The first write is a ruling in force when the second commits, so
    it is in the second's set and on its chain, and not in its own."""
    ruling, other, _ = _seed_case(h)
    sent, page = await _to_roster(h)
    first = await h.name(page, PLAN, sent, [])
    assert first["status"] == "committed", first
    sent, page = await _to_roster(h, FACTS[11])
    second = await h.name(page, FACTS[11], sent, [])
    assert second["status"] == "committed", second

    _, replay, lines = _measure(h, tmp_path)
    one, two = _writes(replay)
    assert (one.commit.record, two.commit.record) == (first["id"], second["id"])
    assert one.commit.end < two.commit.start
    assert one.expected == {ruling, other}
    assert two.expected == {ruling, other, first["id"]}
    assert one.missing == () and two.missing == ()
    assert "  full expected set held: 2 / 2 = 1.000" in lines


async def test_a_ruling_in_force_at_the_commit_and_never_shown_is_a_miss(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another session's ruling lands after the roster's last check and
    before the commit (the race the prediction guards against): the chain
    lacks it, and the miss names it and the door."""
    _seed_case(h)
    sent, page = await _to_roster(h)
    late: list[str] = []
    decide = review.apply_review

    def racing(*args: Any, **kwargs: Any) -> Any:
        late.append(_ruling(h, FACTS[7]))
        return decide(*args, **kwargs)

    monkeypatch.setattr(review, "apply_review", racing)
    res = await h.name(page, PLAN, sent, [])
    assert res["status"] == "committed", res
    assert late and late[0] not in _chain_ids(h)

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert reading.missing == (late[0],)
    assert reading.door == "memory_write"
    assert "  full expected set held: 0 / 1 = 0.000" in lines
    assert f"    miss {res['id']}: missing {late[0]} (door memory_write)" in lines


async def test_a_write_that_is_not_standing_is_not_counted(
    h: Harness, tmp_path: Path
) -> None:
    """A write answered below the threshold commits with no roster and is
    not a standing write; an ambient one answered 0 is, by its category."""
    _seed_case(h)
    first = await h.write(PLAN)
    plain = await h.answer(first, PLAN, standing=0.1)
    assert plain["status"] == "committed", plain
    ambient_text = FACTS[0]
    first = await h.write(ambient_text, category="ambient")
    sent = h.answers(first, standing=0.0)
    page = await h.write(ambient_text, category="ambient", review=sent)
    ambient = await h.name(page, ambient_text, sent, [], category="ambient")
    assert ambient["status"] == "committed", ambient

    _, replay, lines = _measure(h, tmp_path)
    by_id = {r.commit.record: r for r in _writes(replay)}
    assert not by_id[plain["id"]].standing and not by_id[plain["id"]].roster_shown
    assert by_id[ambient["id"]].read and by_id[ambient["id"]].missing == ()
    assert lines[1].startswith(
        "standing writes 1 (committed writes in the window 2, not standing 1;"
    )
    assert (
        "  not standing but shown a roster (informational, no target): 0 of 1" in lines
    )


async def test_a_write_shown_a_roster_then_answered_below_is_counted_apart(
    h: Harness, tmp_path: Path
) -> None:
    """Answered standing, the write is shown the roster; re-sent under it
    with a lower answer it commits not standing (`named` is required of a
    standing write only). It is no standing write, and the informational
    count names it."""
    _seed_case(h)
    sent, page = await _to_roster(h)
    res = await h.name(page, PLAN, {**sent, "standing": 0.1}, [])
    assert res["status"] == "committed", res

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert not reading.standing and reading.roster_shown
    assert (
        "  not standing but shown a roster (informational, no target): 1 of 1" in lines
    )


async def test_held_superseded_declared_and_disabled_rulings_are_not_expected(
    h: Harness, tmp_path: Path
) -> None:
    """None of the four is on the write's chain, so a reader that expected
    any of them would read a miss: a ruling another record supersedes, a
    ruling held since the record replacing it was removed (ENG-200), the
    ruling the write declares it supersedes, and a ruling in a scope the
    session disabled."""
    ruling = _ruling(h, RULING)
    for text in NEAR:
        h.seed(text)
    superseded = _ruling(h, FACTS[3])
    _replace(h, superseded, FACTS[19])
    held = _ruling(h, FACTS[8])
    h.store.tombstone(_replace(h, held, FACTS[17]), "set aside")
    target = _ruling(h, FACTS[20])
    hidden = _ruling(h, FACTS[5], scopes=[VAULT])
    off = await call_tool(
        h.server, "memory_admin", {"action": "disable_scope", "scope": VAULT}
    )
    assert off["disabled_scopes"] == [VAULT], off
    assert standing.held_ids(h.store) == {held}
    left_out = {superseded, held, target, hidden}
    assert set(standing.guarded_ids(h.store, sorted(left_out))) == left_out - {
        superseded
    }

    sent, page = await _to_roster(h, supersedes=[target])
    sent = {**sent, "said": QUOTE}
    res = await h.name(page, PLAN, sent, [], supersedes=[target])
    assert res["status"] == "committed", res
    assert not left_out & _chain_ids(h)

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert reading.commit.declared == (target,)
    assert reading.expected == {ruling}
    assert reading.missing == ()
    assert "  full expected set held: 1 / 1 = 1.000" in lines


async def test_a_probe_sessions_write_and_update_chain_are_left_out(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe's write is told by its row's harness stamp; its update
    chain, whose pages carry none, by the `update` row each page's call
    records next."""
    _seed_case(h)
    monkeypatch.setenv(ENV_HARNESS_SESSION, PROBE)
    sent, page = await _to_roster(h)
    res = await h.name(page, PLAN, sent, [])
    assert res["status"] == "committed", res
    assert h.events("write")[-1]["harness_session"] == PROBE
    plan = h.seed(FACTS[9])
    opened = await call_tool(h.server, "memory_update", {"id": plan, "standing": 0.9})
    assert is_roster(opened), opened
    assert "harness_session" not in h.events(review.REVIEW_ISSUED)[-1]
    monkeypatch.delenv(ENV_HARNESS_SESSION)

    found, replay, lines = _measure(h, tmp_path)
    assert found.probe_writes == 1
    assert _writes(replay) == []
    assert lines[1].startswith(
        "standing writes 0 (committed writes in the window 0, not standing 0; "
        "probe sessions excluded 1;"
    )
    assert lines[
        lines.index(
            "update-door chains (door memory_update: memory_update rosters, not "
            "writes; outside the count):"
        )
        + 1
    ].startswith("  chains opened in the window 0 (probe chains excluded 1),")


async def test_an_update_doors_roster_is_read_apart_from_its_own_commit(
    h: Harness, checkout: Path, tmp_path: Path
) -> None:
    """An edit of a standing memory is shown the roster on its own chain
    (door memory_update). It is no write: it is read on its own lines, the
    memory itself left out though it was a ruling in force, from the
    update's own commit, not from a change another call made to the
    memory after the page, its caller at the update row's worktree
    stamp."""
    _with_recorder(h, worktree_root=str(checkout))
    ruling, other, _ = _seed_case(h)
    plan = _ruling(h, FACTS[5])
    assert plan in standing.standing_ids(h.store, EVERY_RECORD)
    edit = {"id": plan, "content": FACTS[6], "standing": 0.9, "said": QUOTE}
    page = await call_tool(h.server, "memory_update", edit)
    assert is_roster(page), page
    h.store.put_memory(h.store.load_one(plan))
    answers = {"review_id": page["review"]["review_id"], "named": []}
    res = await call_tool(h.server, "memory_update", {**edit, "review": answers})
    assert res["status"] == "committed", res
    rows = h.store.log_rows()
    closed = max(r.seq for r in rows if r.kind == "update")
    puts = [
        r.seq
        for r in rows
        if r.kind == "memory_put" and f'"id":"{plan}"' in r.payload.replace(" ", "")
    ]
    foreign, own = puts[-2], puts[-1]
    assert foreign < own < closed

    _, replay, lines = _measure(h, tmp_path)
    assert _writes(replay) == []
    (update,) = [r for r in replay.readings if r.commit.door == UPDATE_DOOR]
    assert update.read and update.commit.record == plan
    assert update.commit.start == own
    assert update.expected == {ruling, other} and update.missing == ()
    assert lines[1].startswith(
        "standing writes 0 (committed writes in the window 0, not standing 0;"
    )
    assert (
        "  chains opened in the window 1 (probe chains excluded 0), closed by a "
        "committed update 1 (probe update rows excluded 0)"
    ) in lines
    assert (
        "  committed updates read 1 (caller not recorded 0); full expected set "
        "held: 1 / 1 = 1.000"
    ) in lines


# ---------------------------------------------------------------------------
# M6's conflict clause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("telemetry", [True, False])
async def test_a_commit_answering_conflict_on_a_named_ruling_is_m6(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, telemetry: bool
) -> None:
    """With the guard taken out of the write path, a conflict answered on
    the named ruling commits, as a defect would let it. The reader finds
    it in the write row's answers, or, with telemetry off, in the pair the
    review filed. The conflict answered on a record that is not standing,
    which the product files and commits, is not M6."""
    if not telemetry:
        _with_recorder(h, enabled=False)
    ruling, _, near = _seed_case(h)
    monkeypatch.setattr(
        write_handler, "_standing_guard", lambda *a, **k: ([], [], None)
    )
    sent, page = await _to_roster(h)
    assert near[0] in sent["relation"]
    full = await h.name(page, PLAN, sent, [ruling])
    assert full["status"] == "review", full
    answers = {
        **sent,
        "review_id": full["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "conflict", near[0]: "conflict"},
        "named": [ruling],
    }
    res = await h.write(PLAN, review=answers)
    assert res["status"] == "committed", res

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert reading.missing == ()
    assert reading.conflicted == (ruling,)
    source = "answers" if telemetry else "conflict_put"
    assert reading.conflict_source == source
    if not telemetry:
        assert set(reading.commit.filed) == {ruling, near[0]}
    assert (
        f"    conflict {res['id']}: {ruling} (from {source}, door memory_write)"
    ) in lines
    if telemetry:
        assert "  M6 conflict clause (review_answers): 1 of 1 standing writes" in lines
    else:
        assert "  M6 conflict clause (review_answers): n/a (telemetry off)" in lines
        assert (
            "  M6 conflict clause (conflict_put, writes without answers; a lower "
            "bound): 1 of 1 standing writes (detector review); undecided 0"
        ) in "\n".join(lines)


async def test_m6_reads_a_standing_write_whose_caller_is_not_recorded(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain is opened in the checkout; the call that commits stands in
    a directory no view was recorded at, so the write is not read for its
    full set ("caller not recorded"), and its conflict on the named ruling
    is M6 all the same: the guard reads every record, with no admission."""
    ruling, _, _ = _seed_case(h)
    monkeypatch.setattr(
        write_handler, "_standing_guard", lambda *a, **k: ([], [], None)
    )
    sent, page = await _to_roster(h)
    full = await h.name(page, PLAN, sent, [ruling])
    assert full["status"] == "review", full
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    away = Origin(cwd=str(elsewhere), repo=None, branch=None, worktree_root=None)
    monkeypatch.setattr(_handlers, "capture_origin", lambda *a, **k: away)
    answers = {
        **sent,
        "review_id": full["review"]["review_id"],
        "relation": {**sent["relation"], ruling: "conflict"},
        "named": [ruling],
    }
    res = await h.write(PLAN, review=answers)
    assert res["status"] == "committed", res

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert reading.standing and not reading.caller_recorded and not reading.read
    assert reading.expected == frozenset() and reading.missing == ()
    assert reading.conflicted == (ruling,)
    assert "  caller not recorded (not read): 1" in lines
    assert "  full expected set held: 0 / 0 = n/a" in lines
    assert "  M6 conflict clause (review_answers): 1 of 1 standing writes" in lines
    assert (
        f"    conflict {res['id']}: {ruling} (from answers, door memory_write)"
    ) in lines


async def _call_as(h: Harness, client: str, name: str, **kwargs: Any) -> Any:
    """`name` called by a client that declares itself by header, no session
    declared: its own SessionState, the process's log session."""
    tool = h.server._tool_manager.get_tool(name)
    assert tool is not None, name
    return await tool.fn(**kwargs, ctx=fake_ctx(headers={HEADER_CLIENT: client}))


async def test_only_the_writers_own_session_state_disables_its_scopes(
    h: Harness, tmp_path: Path
) -> None:
    """Two clients of one daemon, told apart by their declared client
    (`identity.registry_key`), each disable a scope; neither declares a
    session, so both log under the process's. The writer's roster leaves
    out its own disabled scope only, and so does what the reader expects."""
    h.server = build_server(
        config=h.config,
        store=h.store,
        state=SessionRegistry(),
        recorder=Recorder(store=h.store, session_id="sess-shared"),
    )
    ruling, other, _ = _seed_case(h)
    mine = _ruling(h, FACTS[5], scopes=["vault-a"])
    theirs = _ruling(h, FACTS[8], scopes=["vault-b"])
    for client, scope in (("alpha", "vault-a"), ("beta", "vault-b")):
        off = await _call_as(
            h, client, "memory_admin", action="disable_scope", scope=scope
        )
        assert off["disabled_scopes"] == [scope], off
    first = await _call_as(h, "alpha", "memory_write", content=PLAN, scopes=["tools"])
    sent = h.answers(first, standing=0.9)
    page = await _call_as(
        h, "alpha", "memory_write", content=PLAN, scopes=["tools"], review=sent
    )
    assert is_roster(page), page
    res = await _call_as(
        h,
        "alpha",
        "memory_write",
        content=PLAN,
        scopes=["tools"],
        review={**sent, "review_id": page["review"]["review_id"], "named": []},
    )
    assert res["status"] == "committed", res
    assert theirs in _rows(h, "roster") and mine not in _chain_ids(h)
    sessions = {e.get("session") for e in h.events("scope_disable")}
    assert sessions == {h.events("write")[-1]["session"]} == {"sess-shared"}

    _, replay, lines = _measure(h, tmp_path)
    (reading,) = _writes(replay)
    assert reading.commit.client_key == "client=alpha"
    assert reading.expected == {ruling, other, theirs}
    assert reading.missing == ()
    assert "  full expected set held: 1 / 1 = 1.000" in lines


def test_the_store_the_product_opens_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reader runs on a copy: the store the product would open (here
    BETTERMEMORY_DIR's, under a faked home) is refused, named by another
    path to the same file, before anything opens it."""
    home = tmp_path / "home"
    live = home / "live"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(live))
    Store(live).close()
    alias = tmp_path / "alias"
    alias.symlink_to(live, target_is_directory=True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["read", "--store", str(alias / "memory.sqlite"), "--since", "2026-01-01"],
    )
    with pytest.raises(SystemExit) as raised:
        p6.main()
    assert raised.value.code == 2
    assert "--store names the store the product opens" in capsys.readouterr().err
