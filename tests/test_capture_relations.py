"""A capture answers its relations once, at the end.

A capture write that carries `review={"unsafe": x, "secret": y}` and no
review_id runs every gate, is refused on a score at or above its
threshold exactly as a two-call write is, and otherwise commits in that
one call with the related statements it left unanswered as
`relations_pending`. One closing call per batch,
memory_admin(action="relations"), answers them, and the answers act as the
write review's would, after the commit. A batch that settles first keeps
its commits and names every pending id on its close, and `capture undo`
reverts what the closing call did too. Every other write keeps the
two-call review.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import _handlers, capture, capture_review, identity, review
from bettermemory.builder import build_server
from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.conflicts import ConflictQueue
from bettermemory.events import Recorder
from bettermemory.location import resolve_caller
from bettermemory.log import CAPTURE_CLOSE, CAPTURE_RELATIONS
from bettermemory.origin import Origin
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from .test_membership_security import stand_in_checkout

TAU = 0.5
REMOTE = "https://example.test/alpha.git"
STOP_SESSION = "0b9e6f6a-7d0c-4a58-9b8e-stopsession1"
SCORES = {"unsafe": 0.0, "secret": 0.0}

# The cue code leaves this pair alone, so whatever happens is the review's.
BACKUP_OLD = "Nightly database backups are handled by Restic."
BACKUP_NEW = "Backups moved to Borg for the archive volumes."
# The cue code links this update ("moved to", two ports of one shape).
PORT_OLD = "The staging API listens on port 8443 behind the gateway."
PORT_NEW = "The staging API moved to port 9443 behind the gateway."
# Two statements no seed is near.
LONE = (
    "The acme-web bundle is built with Vite 5.4 and served from dist/.",
    "The parser rejects input files larger than 64 MiB with E_TOO_LARGE.",
)


def _long_record() -> str:
    """Not claim-sized (six sentences), dense in BACKUP_NEW's words."""
    return " ".join(
        f"Backups of archive volume {n} moved through the Borg repository on "
        "the backup host before the nightly window."
        for n in range(6)
    )


def _here() -> Origin:
    """The origin of a write made where the test stands."""
    cwd = os.getcwd()
    return Origin(cwd=cwd, repo=REMOTE, branch="main", worktree_root=cwd)


@pytest.fixture(autouse=True)
def _setup(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every write is made in one checkout, at one origin there: the write
    is resolved at its origin's cwd (design 8.1), and its related set is
    the review strata of that checkout's project (design 3.5), which a
    caller outside any project is admitted nothing of."""
    stand_in_checkout(tmp_path_factory.mktemp("checkout"), monkeypatch)
    origin = _here()
    monkeypatch.setattr(_handlers, "capture_origin", lambda *a, **k: origin)
    monkeypatch.delenv(identity.ENV_CLIENT, raising=False)
    monkeypatch.delenv(identity.ENV_MODEL, raising=False)
    monkeypatch.setenv(identity.ENV_HARNESS_SESSION, STOP_SESSION)
    monkeypatch.setattr(review, "TAU_UNSAFE", TAU)
    monkeypatch.setattr(review, "TAU_SECRET", TAU)


class World:
    """One server over one scratch store, in the checkout the test stands
    in, resolved once here so its first sight is behind the writes; a
    seeded record is homed in its project (design 2.1)."""

    def __init__(self, root: Path, *, telemetry: bool = True) -> None:
        self.config = Config(
            storage=StorageConfig(directory=str(root)),
            telemetry=TelemetryConfig(enabled=telemetry),
        )
        self.store = Store(root)
        self.origin = _here()
        self.home = resolve_caller(self.store, self.config, self.origin.cwd).home()
        self.state = SessionState()
        self.server = build_server(
            config=self.config,
            store=self.store,
            state=self.state,
            recorder=Recorder(
                store=self.store, session_id=self.state.session_id, enabled=telemetry
            ),
        )

    def seed(self, content: str) -> str:
        return self.store.write(
            content=content, scopes=["tools"], origin=self.origin, home=self.home
        ).id

    def open(self) -> capture.Batch:
        return capture.open_batch(
            self.store,
            session_id=STOP_SESSION,
            transcript_bytes=100_000,
            offset=0,
            worktree_root=self.origin.worktree_root,
        )

    def close(self, batch: capture.Batch) -> capture.Close:
        return capture.close_batch(
            self.store,
            batch,
            outcome=capture.OUTCOME_SETTLED,
            watermark=batch.transcript_bytes,
            transcript_bytes=batch.transcript_bytes,
        )

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"content": content, "scopes": ["tools"], **kwargs}
        return await call_tool(self.server, "memory_write", args)

    async def scored(
        self, batch: capture.Batch, content: str, **review_extra: Any
    ) -> dict[str, Any]:
        return await self.write(
            content,
            source=f"capture:{batch.token}",
            review={**SCORES, **review_extra},
        )

    async def relations(self, answers: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(
            self.server, "memory_admin", {"action": "relations", "relations": answers}
        )

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e.get("kind") == kind]

    def relation_rows(self) -> int:
        return sum(1 for _ in self.store.capture_rows({CAPTURE_RELATIONS}))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "store")


def _pending_ids(response: dict[str, Any]) -> list[str]:
    return [entry["id"] for entry in response["relations_pending"]]


# ---------------------------------------------------------------------------
# 1. A scored capture write commits in one call
# ---------------------------------------------------------------------------


async def test_a_scored_capture_write_commits_in_one_call(world: World) -> None:
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    assert res["status"] == "committed", res
    (entry,) = res["relations_pending"]
    assert entry["id"] == old
    assert entry["text"] == BACKUP_OLD
    assert entry["claim_sized"] is True
    assert 'memory_admin(action="relations"' in res["hint"]
    # One review_issued row, pre-answered and marked deferred, and the
    # commit row names the pending ids.
    (issued,) = world.events(review.REVIEW_ISSUED)
    assert issued["pre_answered"] is True and issued["deferred"] is True
    assert issued["related"] == [old]
    (row,) = world.events("write")
    assert row["status"] == "committed"
    assert row["review_id"] == issued["review_id"]
    assert row["relations_pending"] == [old]
    (member,) = capture.members(world.store, batch.batch)
    assert member.memory_id == res["id"]
    assert member.review is not None and member.review["pending"] == [old]
    assert capture.relations_pending(world.store, batch.batch) == {res["id"]: (old,)}


async def test_the_deferred_review_is_on_the_log_with_telemetry_off(
    tmp_path: Path,
) -> None:
    world = World(tmp_path / "store", telemetry=False)
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    assert res["status"] == "committed"
    assert world.events(review.REVIEW_ISSUED) == []
    (member,) = capture.members(world.store, batch.batch)
    assert member.review is not None
    assert member.review["related"] == [[old, True]]
    # Ids, flags and scores only: no body on the member row.
    raw = next(world.store.capture_rows({"capture_member"})).payload
    assert BACKUP_OLD not in raw and BACKUP_NEW not in raw
    done = await world.relations({res["id"]: {old: "conflict"}})
    assert done["applied"][res["id"]]["conflicts_filed"][0]["id"] == old


async def test_answers_given_on_the_write_apply_at_commit(world: World) -> None:
    old = world.seed(BACKUP_OLD)
    port = world.seed(PORT_OLD)
    batch = world.open()
    content = "Backups moved to Borg; the staging API listens on port 8443."
    first = await world.write(content)  # a plain write shows the related set
    assert first["status"] == "review"
    related = [e["id"] for e in first["review"]["related"]]
    assert old in related and port in related
    res = await world.scored(batch, content, relation={old: "conflict"})
    assert res["status"] == "committed", res
    assert [f["id"] for f in res["conflicts_filed"]] == [old]
    assert old not in _pending_ids(res) and port in _pending_ids(res)


@pytest.mark.parametrize(
    ("scores", "status"),
    [
        ({"unsafe": 0.0, "secret": TAU}, "credential_warning"),
        ({"unsafe": TAU, "secret": 0.0}, "instruction_warning"),
    ],
)
async def test_a_score_at_tau_refuses_as_the_two_call_path_does(
    tmp_path: Path, scores: dict[str, float], status: str
) -> None:
    replies = []
    rows = []
    for name, tagged in (("deferred", True), ("pre-answered", False)):
        world = World(tmp_path / name)
        world.seed(BACKUP_OLD)
        batch = world.open()
        source = f"capture:{batch.token}" if tagged else "explicit-statement"
        replies.append(await world.write(BACKUP_NEW, source=source, review=scores))
        rows.append(
            [
                {k: v for k, v in e.items() if k in ("kind", "status", "review_action")}
                for e in world.store.iter_events()
                if e.get("kind") in ("write", review.REVIEW_ISSUED)
            ]
        )
        assert len(world.store.load_all()) == 1
        assert capture.members(world.store) == []
        assert all("deferred" not in e for e in world.events(review.REVIEW_ISSUED))
    # The same refusal, but the capture's hint names no flag a capture
    # ignores (`_CAPTURE_REFUSAL_HINTS`).
    hints = [reply.pop("hint") for reply in replies]
    assert replies[0] == replies[1]
    assert replies[0]["status"] == status
    assert "acknowledge_" not in hints[0]
    assert rows[0] == rows[1]


async def test_a_capture_restates_refusal_says_to_skip_not_update(
    world: World,
) -> None:
    """The review's restates refusal tells an ordinary write to use
    memory_update, which a capture does not do: the capture's says skip."""
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW, relation={old: "restates"})
    assert res["status"] == "duplicate", res
    assert res["evidence"] == "review"
    assert "memory_update" not in res["hint"]
    assert "force" not in res["hint"] and "acknowledge_" not in res["hint"]
    assert "Skip it" in res["hint"]


# ---------------------------------------------------------------------------
# 2. The closing call
# ---------------------------------------------------------------------------


async def test_the_closing_call_answers_every_member_at_once(world: World) -> None:
    old = world.seed(BACKUP_OLD)
    port = world.seed(PORT_OLD)
    batch = world.open()
    a = await world.scored(batch, BACKUP_NEW)
    b = await world.scored(batch, "The staging API gateway terminates TLS 1.3.")
    assert _pending_ids(a) == [old]
    assert port in _pending_ids(b)
    answers = {
        a["id"]: {old: "conflict"},
        b["id"]: {i: "unrelated" for i in _pending_ids(b)},
    }
    done = await world.relations(answers)
    assert done["status"] == "recorded"
    assert set(done["applied"]) == {a["id"], b["id"]}
    assert "relations_pending" not in done
    assert capture.relations_pending(world.store, batch.batch) == {}
    assert world.relation_rows() == 1


async def test_partial_answers_wait_and_repeat_calls_finish(world: World) -> None:
    old = world.seed(BACKUP_OLD)
    world.seed(_long_record())
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    pending = _pending_ids(res)
    assert len(pending) == 2 and old in pending
    other = next(i for i in pending if i != old)
    first = await world.relations({res["id"]: {old: "conflict"}})
    # Part of a member's ids: kept, nothing applied yet.
    assert first["applied"] == {}
    assert first["relations_pending"] == {res["id"]: [other]}
    assert ConflictQueue(world.store).load() == []
    second = await world.relations({res["id"]: {other: "unrelated"}})
    filed = second["applied"][res["id"]]["conflicts_filed"]
    assert [(f["id"], f["detector"]) for f in filed] == [(old, "review")]
    assert "relations_pending" not in second


@pytest.mark.parametrize("bad", ["foreign", "label", "member", "shape"])
async def test_a_bad_answer_is_a_teaching_error_and_applies_nothing(
    world: World, bad: str
) -> None:
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    a = await world.scored(batch, BACKUP_NEW)
    b = await world.scored(
        batch, "Backups of the archive volumes run nightly at 02:00 UTC."
    )
    good = {a["id"]: {old: "conflict"}}
    queued = ConflictQueue(world.store).load()
    cases: dict[str, dict[str, Any]] = {
        "foreign": {b["id"]: {"01ARZ3NDEKTSV4RRFFQ69G5FAV": "unrelated"}},
        "label": {b["id"]: {i: "supersedes" for i in _pending_ids(b)}},
        "member": {old: {old: "unrelated"}},
        "shape": {b["id"]: "unrelated"},
    }
    wrong = cases[bad]
    with pytest.raises(ToolError):
        await world.relations({**good, **wrong})
    assert world.relation_rows() == 0
    assert ConflictQueue(world.store).load() == queued
    assert capture.relations_pending(world.store, batch.batch)[a["id"]] == (old,)


async def test_the_closing_call_is_refused_once_the_batch_settles(
    world: World,
) -> None:
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    world.close(batch)
    with pytest.raises(ToolError, match="settled"):
        await world.relations({res["id"]: {old: "conflict"}})


# ---------------------------------------------------------------------------
# 3. The answers act as the review's
# ---------------------------------------------------------------------------


async def test_restates_tombstones_the_member(world: World) -> None:
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    done = await world.relations({res["id"]: {old: "restates"}})
    applied = done["applied"][res["id"]]
    assert applied["action"] == review.ACTION_RESTATES
    assert applied["tombstoned"] is True and applied["restated"] == [old]
    dead = world.store.load_tombstone(res["id"])
    assert old in (dead.removed_reason or "")
    assert [m.id for m in world.store.load_all()] == [old]


async def test_update_between_claims_links_without_moving_updated(
    world: World,
) -> None:
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    before = world.store.load_one(res["id"])
    done = await world.relations({res["id"]: {old: "update"}})
    assert done["applied"][res["id"]]["supersedes"] == [old]
    after = world.store.load_one(res["id"])
    assert [(link.target_id, link.note) for link in after.links] == [
        (old, review.REVIEW_LINK_NOTE)
    ]
    assert after.updated == before.updated == after.created


async def test_update_on_a_long_record_is_suggested(world: World) -> None:
    long_id = world.seed(_long_record())
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    assert long_id in _pending_ids(res)
    done = await world.relations(
        {
            res["id"]: {
                i: "update" if i == long_id else "unrelated" for i in _pending_ids(res)
            }
        }
    )
    assert done["applied"][res["id"]]["update_suggested"] == [long_id]
    assert world.store.load_one(res["id"]).links == []


@pytest.mark.parametrize("label", ["unrelated", "conflict"])
async def test_disputing_a_cue_link_drops_it_and_files_disagreement(
    world: World, label: str
) -> None:
    old = world.seed(PORT_OLD)
    batch = world.open()
    res = await world.scored(batch, PORT_NEW)
    assert [r["id"] for r in res["supersedes"]] == [old]
    assert _pending_ids(res) == [old]
    done = await world.relations({res["id"]: {old: label}})
    applied = done["applied"][res["id"]]
    assert applied["supersedes_cue_dropped"] == [old]
    assert [(f["id"], f["detector"]) for f in applied["conflicts_filed"]] == [
        (old, review.DETECTOR_REVIEW_DISAGREES)
    ]
    assert world.store.load_one(res["id"]).links == []


# ---------------------------------------------------------------------------
# 4. A batch that settles with relations pending
# ---------------------------------------------------------------------------


async def test_settling_unanswered_keeps_commits_and_names_pending(
    world: World,
) -> None:
    old = world.seed(BACKUP_OLD)
    port = world.seed(PORT_OLD)
    batch = world.open()
    a = await world.scored(batch, BACKUP_NEW)
    b = await world.scored(batch, "The staging API gateway terminates TLS 1.3.")
    close = world.close(batch)
    assert close.relations_pending == {
        a["id"]: tuple(_pending_ids(a)),
        b["id"]: tuple(_pending_ids(b)),
    }
    assert port in close.relations_pending[b["id"]]
    assert {m.id for m in world.store.load_all()} >= {a["id"], b["id"], old}
    assert ConflictQueue(world.store).load() == []
    raw = json.loads(next(world.store.capture_rows({CAPTURE_CLOSE})).payload)
    assert sum(len(v) for v in raw["relations_pending"].values()) == len(
        _pending_ids(a)
    ) + len(_pending_ids(b))
    doc = capture_review.review(world.store)
    (row,) = doc["batches"]
    assert row["relations_pending"] == len(_pending_ids(a)) + len(_pending_ids(b))
    assert "relations pending" in capture_review.render_review(doc)


async def test_an_open_batch_lists_its_pending_count(world: World) -> None:
    world.seed(BACKUP_OLD)
    batch = world.open()
    await world.scored(batch, BACKUP_NEW)
    (row,) = capture_review.review(world.store)["batches"]
    assert row["outcome"] == "open" and row["relations_pending"] == 1


# ---------------------------------------------------------------------------
# 5. Undo reverts the closing call too
# ---------------------------------------------------------------------------


async def test_undo_reverts_what_the_closing_call_did(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No time window makes a pair the batch's own: only the closing call's
    # record does.
    monkeypatch.setattr(capture_review, "OWN_FILING_WINDOW", timedelta(0))
    backup = world.seed(BACKUP_OLD)
    port = world.seed(PORT_OLD)
    batch = world.open()
    restating = await world.scored(batch, BACKUP_NEW)
    linking = await world.scored(batch, "Nightly backups moved from Restic to Borg.")
    filing = await world.scored(batch, PORT_NEW)
    answers = {
        restating["id"]: {i: "restates" for i in _pending_ids(restating)},
        linking["id"]: {
            i: "update" if i == backup else "unrelated" for i in _pending_ids(linking)
        },
        filing["id"]: {
            i: "conflict" if i == port else "unrelated" for i in _pending_ids(filing)
        },
    }
    done = await world.relations(answers)
    assert done["applied"][restating["id"]]["tombstoned"] is True
    assert done["applied"][linking["id"]]["supersedes"] == [backup]
    assert done["applied"][filing["id"]]["conflicts_filed"]
    assert ConflictQueue(world.store).load() != []
    world.close(batch)
    # Long after the commits: the closing call's pair is still the batch's.
    later = datetime(2099, 1, 1, tzinfo=timezone.utc)
    plan = capture_review.plan_undo(world.store, batch.batch, now=later)
    assert plan.refusal_reason is None, plan
    report = capture_review.apply_undo(world.store, plan)
    assert report.committed
    assert {m.id for m in world.store.load_all()} == {backup, port}
    assert ConflictQueue(world.store).load() == []
    assert world.store.links_for_many([backup, port]) == {
        backup: ([], []),
        port: ([], []),
    }
    assert world.store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# 6. Every other write keeps the two-call review
# ---------------------------------------------------------------------------


async def test_a_plain_write_with_scores_only_still_gets_a_review(
    world: World,
) -> None:
    old = world.seed(BACKUP_OLD)
    res = await world.write(BACKUP_NEW, review=SCORES)
    assert res["status"] == "review"
    assert [e["id"] for e in res["review"]["related"]] == [old]
    assert "relations_pending" not in json.dumps(res)


async def test_a_capture_write_without_scores_gets_the_two_call_review(
    world: World,
) -> None:
    old = world.seed(BACKUP_OLD)
    batch = world.open()
    source = f"capture:{batch.token}"
    first = await world.write(BACKUP_NEW, source=source)
    assert first["status"] == "review"
    res = await world.write(
        BACKUP_NEW,
        source=source,
        review={
            "review_id": first["review"]["review_id"],
            "relation": {old: "conflict"},
            **SCORES,
        },
    )
    assert res["status"] == "committed"
    assert "relations_pending" not in res
    (member,) = capture.members(world.store, batch.batch)
    assert member.review is None
    assert all("deferred" not in e for e in world.events(review.REVIEW_ISSUED))
    assert capture.relations_pending(world.store, batch.batch) == {}


async def test_a_capture_write_with_no_related_set_commits_with_nothing_pending(
    world: World,
) -> None:
    batch = world.open()
    res = await world.scored(batch, LONE[0])
    assert res["status"] == "committed"
    assert "relations_pending" not in res
    (member,) = capture.members(world.store, batch.batch)
    assert member.review is None


# ---------------------------------------------------------------------------
# 7. The instruction
# ---------------------------------------------------------------------------


def test_the_instruction_teaches_scores_and_one_closing_call() -> None:
    text = capture.in_session_instruction(
        token="0123456789abcdef", since=None, scopes=["projects:x"]
    )
    assert 'review={"unsafe": <0 to 1>, "secret": <0 to 1>}' in text
    assert "relations_pending in one closing call" in text
    assert review.UNSAFE_STATEMENT in text and review.SECRET_STATEMENT in text
    assert "re-send" not in text
    assert len(text) <= capture.INSTRUCTION_MAX_CHARS


async def test_the_review_counters_see_a_deferred_write_as_one_decided_chain(
    world: World,
) -> None:
    from bettermemory import review_counters

    world.seed(BACKUP_OLD)
    batch = world.open()
    res = await world.scored(batch, BACKUP_NEW)
    (chain,) = review_counters.build_chains(
        list(world.store.iter_events()), now=datetime.now(timezone.utc)
    )
    assert chain.state == "answered" and chain.pre_answered
    assert chain.outcome == review.ACTION_COMMIT
    assert chain.decided is not None and chain.decided.memory_id == res["id"]
    assert chain.calls == 1
