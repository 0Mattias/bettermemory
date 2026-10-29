"""Capture batches on the log, and the capture write.

A capture is a batch: `capture_open` with an unguessable token, a
`capture_member` row per write the token commits, `capture_close` with
the transcript offset it captured to. The token is the binding (a write
without it is never in the batch, whoever sends it), and the write's
sessions are evidence: the harness session must be the batch's Stop
session, or one the /clear bridge joins to it. Everything capture keeps
across a restart is read back from these rows, which are control rows,
so they exist with telemetry off.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _handlers, capture, identity
from bettermemory.builder import build_server
from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.events import Recorder
from bettermemory.handlers._shared import _validate_write_payload
from bettermemory.log import (
    CAPTURE_CLOSE,
    CAPTURE_KINDS,
    CAPTURE_MEMBER,
    CAPTURE_OPEN,
    CONTROL_KINDS,
)
from bettermemory.origin import Origin
from bettermemory.session import SessionState
from bettermemory.store import MEMBERSHIP_EVENT_KINDS, MEMBERSHIP_MUTATION_KINDS, Store

from ._mcp import call_tool

WORKTREE = "/work/alpha"
ORIGIN = Origin(
    cwd=WORKTREE,
    repo="https://example.test/alpha.git",
    branch="main",
    worktree_root=WORKTREE,
)
STOP_SESSION = "0b9e6f6a-7d0c-4a58-9b8e-stopsession1"
BEFORE_CLEAR = "0b9e6f6a-7d0c-4a58-9b8e-beforeclear1"
OTHER_WINDOW = "0b9e6f6a-7d0c-4a58-9b8e-otherwindow1"
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)

BODY = "The bettermemory daemon binds 127.0.0.1 only and listens on port 7397."


@pytest.fixture(autouse=True)
def _fixed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write and event carries the same worktree, so the bridge's
    worktree anchor does not depend on where the suite runs."""
    monkeypatch.setattr(_handlers, "capture_origin", lambda *a, **k: ORIGIN)
    monkeypatch.delenv(identity.ENV_CLIENT, raising=False)
    monkeypatch.delenv(identity.ENV_MODEL, raising=False)


class World:
    """One in-process server over one scratch store. The harness session
    comes from the environment there, as for a server Claude Code started."""

    def __init__(self, root: Path, *, telemetry: bool = True) -> None:
        self.root = root
        self.config = Config(
            storage=StorageConfig(directory=str(root)),
            telemetry=TelemetryConfig(enabled=telemetry),
        )
        self.store = Store(root)
        self.state = SessionState()
        self.server = build_server(
            config=self.config,
            store=self.store,
            state=self.state,
            recorder=Recorder(
                store=self.store, session_id=self.state.session_id, enabled=telemetry
            ),
        )

    def open(self, session_id: str = STOP_SESSION, **kw: Any) -> capture.Batch:
        kw.setdefault("transcript_bytes", 100_000)
        kw.setdefault("offset", 0)
        kw.setdefault("worktree_root", WORKTREE)
        return capture.open_batch(self.store, session_id=session_id, **kw)

    async def write(self, content: str = BODY, **kwargs: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"content": content, "scopes": ["tools"]}
        args.update(kwargs)
        return await call_tool(self.server, "memory_write", args)

    async def capture_write(
        self, batch: capture.Batch, content: str = BODY, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.write(content, source=f"capture:{batch.token}", **kwargs)

    def kinds(self) -> list[str]:
        return [row.kind for row in self.store.log_rows()]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv(identity.ENV_HARNESS_SESSION, STOP_SESSION)
    return World(tmp_path / "store")


# ---------------------------------------------------------------------------
# The token is the binding
# ---------------------------------------------------------------------------


async def test_a_capture_write_with_the_token_joins_its_batch(world: World) -> None:
    batch = world.open()
    result = await world.capture_write(batch)
    assert result["status"] == "committed", result
    memory = world.store.load_one(result["id"])
    # A captured memory is the model's inference from the conversation.
    assert memory.source.value == "inferred"
    [member] = capture.members(world.store, batch.batch)
    assert member.memory_id == memory.id
    assert member.body_hash == capture.body_hash(BODY)
    assert member.harness_session == STOP_SESSION
    assert member.server_session == world.state.session_id
    assert member.joined_by == capture.JOINED_BY_STOP_SESSION
    # The member row commits with the memory: after its put only the
    # membership rows of the same write (its home, a HEAD witness) come
    # before it, and the home row names this memory.
    rows = world.store.log_rows()
    put = next(r for r in rows if r.kind == "memory_put" and memory.id in r.payload)
    after = rows[rows.index(put) + 1 :]
    member_at = next(i for i, r in enumerate(after) if r.kind == CAPTURE_MEMBER)
    between = after[:member_at]
    assert {r.kind for r in between} <= (
        MEMBERSHIP_MUTATION_KINDS | MEMBERSHIP_EVENT_KINDS
    ), [r.kind for r in between]
    assert any(r.kind == "home_put" and memory.id in r.payload for r in between)
    assert memory.id in after[member_at].payload
    committed = [e for e in world.store.iter_events() if e.get("status") == "committed"]
    assert committed[-1]["capture_batch"] == batch.batch


async def test_a_write_without_a_valid_token_is_refused(world: World) -> None:
    world.open()
    for source in ("capture:", "capture:0000000000000000", "capture:not-a-token"):
        result = await world.write(source=source)
        assert result["status"] == "capture_refused", (source, result)
        assert result["rule"] == capture.TOKEN_UNKNOWN
        assert "capture:" in result["hint"]
    assert world.store.count_memories() == 0
    refused = [e for e in world.store.iter_events() if e.get("kind") == "write"]
    assert {e["capture_rule"] for e in refused} == {capture.TOKEN_UNKNOWN}


async def test_a_subagent_shaped_write_is_never_in_the_batch(world: World) -> None:
    """A background subagent shares the session's harness id and server
    session but never sees the instruction, so it has no token. Its write
    behaves exactly as any write does today, and is not the batch's."""
    batch = world.open()
    result = await world.write()
    assert result["status"] == "committed"
    assert world.store.load_one(result["id"]).source.value == "explicit-statement"
    assert capture.members(world.store, batch.batch) == []
    assert CAPTURE_MEMBER not in world.kinds()


async def test_a_closed_or_expired_batch_takes_no_more_writes(world: World) -> None:
    settled = world.open()
    capture.close_batch(
        world.store,
        settled,
        outcome=capture.OUTCOME_SETTLED,
        watermark=100_000,
        transcript_bytes=100_000,
    )
    result = await world.capture_write(settled)
    assert (result["status"], result["rule"]) == (
        "capture_refused",
        capture.BATCH_CLOSED,
    )
    stale = world.open(now=datetime.now(timezone.utc) - capture.BATCH_TTL)
    result = await world.capture_write(stale)
    assert result["rule"] == capture.BATCH_EXPIRED
    assert world.store.count_memories() == 0


def test_a_refusal_inside_a_capture_ends_it_rather_than_routing_around() -> None:
    """A write refused by a closed, expired or mismatched batch must not
    be re-sent without the tag, which would skip the capture rules, the
    undone gate and membership. Only an unknown token may mean the model
    was never asked to capture."""
    from bettermemory.handlers.write import _CAPTURE_TOKEN_HINTS

    inside = (
        capture.BATCH_CLOSED,
        capture.BATCH_EXPIRED,
        "capture_session_mismatch",
    )
    assert set(_CAPTURE_TOKEN_HINTS) == {capture.TOKEN_UNKNOWN, *inside}
    for rule in inside:
        hint = _CAPTURE_TOKEN_HINTS[rule]
        assert "Drop the" not in hint and "ordinary memory" not in hint, rule
        assert "Do not write it without the capture: source" in hint, rule
        assert "End the capture" in hint, rule
    unknown = _CAPTURE_TOKEN_HINTS[capture.TOKEN_UNKNOWN]
    assert "If you were not asked to capture, drop the capture: source" in unknown
    assert "Copy the token exactly" in unknown


async def test_a_clear_shaped_write_is_joined_by_the_bridge(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a /clear the shim still forwards the id it was started with,
    while the Stop names the new one. The server's earlier rows, stamped
    with the old id in this worktree, are what the hooks' bridge joins."""
    monkeypatch.setenv(identity.ENV_HARNESS_SESSION, BEFORE_CLEAR)
    await call_tool(world.server, "memory_search", {"query": "daemon port"})
    batch = world.open(session_id=STOP_SESSION)
    result = await world.capture_write(batch)
    assert result["status"] == "committed", result
    [member] = capture.members(world.store, batch.batch)
    assert member.harness_session == BEFORE_CLEAR
    assert member.joined_by == capture.JOINED_BY_BRIDGE


async def test_another_windows_write_with_a_leaked_token_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server serving another window, whose hooks run beside the Stop
    session's, is never the bridge: a token copied there does not admit
    its write."""
    world.store.record_event(
        "turn_audited", session=STOP_SESSION, triggered_from="stop_hook"
    )
    world.store.record_event(
        "turn_audited", session=OTHER_WINDOW, triggered_from="stop_hook"
    )
    monkeypatch.setenv(identity.ENV_HARNESS_SESSION, OTHER_WINDOW)
    await call_tool(world.server, "memory_search", {"query": "daemon port"})
    batch = world.open(session_id=STOP_SESSION)
    result = await world.capture_write(batch)
    assert (result["status"], result["rule"]) == (
        "capture_refused",
        "capture_session_mismatch",
    )
    assert world.store.count_memories() == 0


def test_the_tagged_source_is_accepted_only_for_an_open_batch() -> None:
    kwargs: dict[str, Any] = {
        "content": BODY,
        "scopes": ["tools"],
        "confidence": "medium",
        "source": "capture:0123456789abcdef",
        "allowed_scopes": [],
    }
    with pytest.raises(ValueError, match="names a capture batch"):
        _validate_write_payload(**kwargs)
    assert _validate_write_payload(**kwargs, capture=True)["source"].value == (
        "inferred"
    )


# ---------------------------------------------------------------------------
# The capture gates and the flags a capture ignores
# ---------------------------------------------------------------------------


async def test_capture_rules_refuse_with_the_rule_and_no_flag_passes(
    world: World,
) -> None:
    batch = world.open()
    body = "The assistant said the daemon binds 127.0.0.1 only."
    result = await world.capture_write(
        batch,
        body,
        force=True,
        acknowledge_transient=True,
        acknowledge_user_claim=True,
        acknowledge_scope_mismatch=True,
    )
    assert result["status"] == "capture_rule", result
    assert result["rules"][0]["rule"] == 3
    assert result["rules"][0]["name"] == "plain_subject"
    assert "assistant said" in result["rules"][0]["snippet"]
    # The same body without the token is an ordinary write, as today.
    assert (await world.write(body))["status"] == "committed"


async def test_a_capture_about_another_project_takes_its_own_scope(
    world: World,
) -> None:
    """The scratch proof: a chat in the bettermemory folder about kettle, a
    tea timer. The folder's project is known to the store (a memory under
    `projects:bettermemory` written here), and a capture write filed under
    `projects:kettle` commits with that scope alone."""
    seeded = await world.write(
        "The bettermemory daemon binds 127.0.0.1 only.",
        scopes=["projects:bettermemory"],
    )
    assert seeded["status"] == "committed", seeded
    batch = world.open()
    body = "kettle, the tea timer, rings three times when a steep ends."
    result = await world.capture_write(batch, body, scopes=["projects:kettle"])
    assert result["status"] == "committed", result
    assert world.store.load_one(result["id"]).scopes == ["projects:kettle"]


async def test_a_capture_scope_mismatch_is_answered_by_the_scope_it_suggests(
    world: World,
) -> None:
    """A capture body that also names the folder's project is refused as a
    scope mismatch. The flag the general hint offers is ignored on a
    capture, so the capture's hint does not offer it; adding the suggested
    scope is the answer, and the same write then commits."""
    await world.write(
        "The bettermemory daemon binds 127.0.0.1 only.",
        scopes=["projects:bettermemory"],
    )
    batch = world.open()
    body = "kettle, the tea timer, keeps its presets in a bettermemory store."
    refused = await world.capture_write(batch, body, scopes=["projects:kettle"])
    assert refused["status"] == "scope_mismatch", refused
    assert "projects:bettermemory" in refused["suggested_scopes"]
    assert "acknowledge_" not in refused["hint"]
    assert "suggested_scopes" in refused["hint"]
    again = await world.capture_write(
        batch, body, scopes=["projects:kettle"], acknowledge_scope_mismatch=True
    )
    assert again["status"] == "scope_mismatch", again
    answered = await world.capture_write(
        batch, body, scopes=["projects:kettle", "projects:bettermemory"]
    )
    assert answered["status"] == "committed", answered
    [member] = capture.members(world.store, batch.batch)
    assert member.memory_id == answered["id"]


async def test_the_transient_and_credential_flags_are_ignored(world: World) -> None:
    batch = world.open()
    transient = await world.capture_write(
        batch,
        "The bettermemory daemon is currently bound to port 7397.",
        acknowledge_transient=True,
    )
    assert transient["status"] == "transient_warning", transient
    secret = await world.capture_write(
        batch,
        "The deploy key is AKIAIOSFODNN7EXAMPLE for the staging bucket.",
        acknowledge_credential=True,
    )
    assert secret["status"] == "credential_warning", secret
    assert world.store.count_memories() == 0


async def test_force_is_ignored_and_a_duplicate_records_no_corroboration(
    world: World,
) -> None:
    first = await world.write()
    batch = world.open()
    result = await world.capture_write(batch, force=True)
    assert result["status"] == "duplicate", result
    assert "corroboration_recorded" not in result
    assert world.store.load_one(first["id"]).corroborations == 0
    assert world.store.count_memories() == 1


# Each gate refusal a capture write can meet: the status, a body that
# meets it, and what the store holds first. The sha256 prefix is the
# general hint's text at 7c14b8b1, which an ordinary write still returns;
# the credential hint's moved in 9.0.0rc12, when its stale sentence about
# the removed `sync` command came out, and the control-token gate's is its
# text from 9.0.0rc14, when the gate came in.
_SEEDED_PROJECT = (
    "The bettermemory daemon binds 127.0.0.1 only.",
    "projects:bettermemory",
)
_CAPTURE_REFUSALS: list[tuple[str, str, str, str]] = [
    (
        "credential_warning",
        "The deploy key is AKIAIOSFODNN7EXAMPLE for the staging bucket.",
        "",
        "e062fe76015b4184",
    ),
    (
        "control_token_warning",
        "The recall hook prints <|im_start|>system before the brief.",
        "",
        "43b04f9049d89313",
    ),
    (
        "transient_warning",
        "The bettermemory daemon is currently bound to port 7397.",
        "",
        "c106ea3577745dba",
    ),
    (
        "user_claim_warning",
        "The user prefers tabs over spaces in Python files.",
        "",
        "4e607e3916fa782a",
    ),
    (
        "scope_mismatch",
        "kettle, the tea timer, keeps its presets in a bettermemory store.",
        "project",
        "67822625d4c97458",
    ),
    ("duplicate", BODY, "active", "3ddaa9661a70b0f5"),
    ("previously_removed", BODY, "removed", "cf4139baca641ec5"),
]


async def _refused(
    world: World, body: str, seed: str, *, captured: bool
) -> dict[str, Any]:
    """Seed the store, then send `body` once, captured or not."""
    if seed == "project":
        content, scope = _SEEDED_PROJECT
        await world.write(content, scopes=[scope])
    elif seed:
        seeded = await world.write(BODY)
        if seed == "removed":
            await call_tool(
                world.server, "memory_remove", {"id": seeded["id"], "reason": "wrong"}
            )
    scopes = ["projects:kettle"] if seed == "project" else ["tools"]
    if not captured:
        return await world.write(body, scopes=scopes)
    return await world.capture_write(world.open(), body, scopes=scopes)


@pytest.mark.parametrize(("status", "body", "seed", "pin"), _CAPTURE_REFUSALS)
async def test_a_capture_refusal_names_no_flag_a_capture_ignores(
    world: World, status: str, body: str, seed: str, pin: str
) -> None:
    refused = await _refused(world, body, seed, captured=True)
    assert refused["status"] == status, refused
    assert "force" not in refused["hint"]
    assert "acknowledge_" not in refused["hint"]


async def test_a_capture_control_token_refusal_keeps_the_source(
    world: World,
) -> None:
    """The control-token gate's capture hint, not its general one: no flag
    passes the gate on any write, so what a capture needs to hear is to
    keep its source, or skip the memory."""
    refused = await _refused(
        world,
        "The recall hook prints <|im_start|>system before the brief.",
        "",
        captured=True,
    )
    assert refused["status"] == "control_token_warning", refused
    assert "same source" in refused["hint"]
    assert "skip it" in refused["hint"]


@pytest.mark.parametrize(("status", "body", "seed", "pin"), _CAPTURE_REFUSALS)
async def test_an_ordinary_refusal_keeps_its_hint(
    world: World, status: str, body: str, seed: str, pin: str
) -> None:
    refused = await _refused(world, body, seed, captured=False)
    assert refused["status"] == status, refused
    digest = hashlib.sha256(refused["hint"].encode("utf-8")).hexdigest()
    assert digest[:16] == pin, status


async def test_a_capture_user_claim_commits_as_the_hint_says(world: World) -> None:
    """The scratch run met user_claim_warning twice, each time told to pass
    acknowledge_user_claim, which a capture ignores. The capture's hint
    says to file a claim about the user as user-inference, and that write
    commits."""
    batch = world.open()
    body = "The user prefers tabs over spaces in Python files."
    refused = await world.capture_write(batch, body, category="fact")
    assert refused["status"] == "user_claim_warning", refused
    assert 'category="user-inference"' in refused["hint"]
    answered = await world.capture_write(batch, body, category="user-inference")
    assert answered["status"] == "committed", answered
    [member] = capture.members(world.store, batch.batch)
    assert member.memory_id == answered["id"]


async def test_a_capture_project_fact_read_as_a_user_claim_is_rephrased(
    world: World,
) -> None:
    """The scratch run's two refusals were project facts put as what the
    user wants. The capture's hint leads with the rephrase, and the
    rephrased fact commits as a fact."""
    batch = world.open()
    refused = await world.capture_write(
        batch,
        "The user wants kettle to ship as a single static binary under 2 MB.",
        category="fact",
    )
    assert refused["status"] == "user_claim_warning", refused
    assert refused["hint"].index("rephrase") < refused["hint"].index("user-inference")
    answered = await world.capture_write(
        batch, "kettle ships as a single static binary under 2 MB.", category="fact"
    )
    assert answered["status"] == "committed", answered


async def test_an_undone_body_is_refused_as_previously_undone(world: World) -> None:
    batch = world.open()
    written = await world.capture_write(batch)
    capture.close_batch(
        world.store,
        batch,
        outcome=capture.OUTCOME_SETTLED,
        watermark=1,
        transcript_bytes=1,
    )
    capture.record_undo(world.store, batch.batch, [written["id"]])
    again = world.open()
    respaced = (
        "  the BETTERMEMORY daemon binds 127.0.0.1 only and\nlistens on port 7397. "
    )
    result = await world.capture_write(again, respaced)
    assert (result["status"], result["rule"]) == (
        "capture_refused",
        "previously_undone",
    )
    # The owner's undo binds captures only; an ordinary write is untouched.
    assert (await world.write(respaced, force=True))["status"] == "committed"


async def test_capture_rows_exist_with_telemetry_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(identity.ENV_HARNESS_SESSION, STOP_SESSION)
    world = World(tmp_path / "quiet", telemetry=False)
    batch = world.open()
    result = await world.capture_write(batch)
    assert result["status"] == "committed"
    capture.close_batch(
        world.store,
        batch,
        outcome=capture.OUTCOME_SETTLED,
        watermark=5,
        transcript_bytes=5,
    )
    assert list(world.store.iter_events()) == []
    assert [k for k in world.kinds() if k in CAPTURE_KINDS] == [
        CAPTURE_OPEN,
        CAPTURE_MEMBER,
        CAPTURE_CLOSE,
    ]


# ---------------------------------------------------------------------------
# The rows: control kinds, atomic opening, and a restart reads them back
# ---------------------------------------------------------------------------


def test_capture_rows_are_control_rows_with_their_own_writer(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    assert CAPTURE_KINDS <= CONTROL_KINDS
    with pytest.raises(ValueError, match="control kind"):
        store.record_event(CAPTURE_OPEN)
    with pytest.raises(ValueError, match="not a capture kind"):
        store.record_capture("write", {})
    batch = capture.open_batch(
        store, session_id=STOP_SESSION, transcript_bytes=10, offset=0
    )
    assert list(store.iter_events()) == []
    assert [r.kind for r in store.capture_rows({CAPTURE_OPEN})] == [CAPTURE_OPEN]
    assert batch.token and len(batch.token) == 16
    assert store.log_verify()["status"] == "ok"
    assert not store.refold()["problems"]


def test_a_stored_token_an_editor_made_non_ascii_is_unknown(tmp_path: Path) -> None:
    """The batch token is read off the log, which an editor of the store
    file can rewrite. Compared as text, a non-ASCII one ended `capture
    review` and `undo` in a TypeError; it reads as no match."""
    import json
    import sqlite3

    store = Store(tmp_path / "store")
    batch = capture.open_batch(
        store, session_id=STOP_SESSION, transcript_bytes=10, offset=0
    )
    row = next(store.capture_rows({CAPTURE_OPEN}))
    payload = json.loads(row.payload)
    payload["token"] = "\u00e9" * 16
    outside = sqlite3.connect(store.path)
    outside.execute(
        "UPDATE log SET payload = ? WHERE seq = ?", (json.dumps(payload), row.seq)
    )
    outside.commit()
    outside.close()
    assert capture.batch_for_token(store, batch.token) == (None, capture.TOKEN_UNKNOWN)


def test_opening_is_atomic_per_session(tmp_path: Path) -> None:
    path = tmp_path / "store"
    store = Store(path)
    first = capture.open_batch(
        store, session_id=STOP_SESSION, transcript_bytes=10, offset=0
    )
    # A second Stop for the session, on another connection, opens nothing.
    other = Store(path)
    with pytest.raises(capture.BatchAlreadyOpenError):
        capture.open_batch(
            other, session_id=STOP_SESSION, transcript_bytes=10, offset=0
        )
    assert len(capture.batches(store)) == 1
    # Another session opens its own; the first opens again once settled.
    capture.open_batch(other, session_id=OTHER_WINDOW, transcript_bytes=10, offset=0)
    capture.close_batch(
        store,
        first,
        outcome=capture.OUTCOME_INTERRUPTED,
        watermark=0,
        transcript_bytes=10,
    )
    with pytest.raises(capture.BatchClosedError):
        capture.close_batch(
            other,
            first,
            outcome=capture.OUTCOME_SETTLED,
            watermark=10,
            transcript_bytes=10,
        )
    second = capture.open_batch(
        other, session_id=STOP_SESSION, transcript_bytes=20, offset=0
    )
    assert second.token != first.token
    assert capture.open_batch_for_session(store, STOP_SESSION) == second


def test_the_watermark_and_daily_count_are_rebuilt_from_the_log(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store"
    store = Store(path)
    yesterday = NOW - timedelta(days=1)
    old = capture.open_batch(
        store, session_id=STOP_SESSION, transcript_bytes=40, offset=0, now=yesterday
    )
    capture.close_batch(
        store,
        old,
        outcome=capture.OUTCOME_SETTLED,
        watermark=40,
        transcript_bytes=40,
        now=yesterday,
    )
    settled = capture.open_batch(
        store, session_id=STOP_SESSION, transcript_bytes=900, offset=40, now=NOW
    )
    capture.close_batch(
        store,
        settled,
        outcome=capture.OUTCOME_SETTLED,
        watermark=900,
        transcript_bytes=950,
        now=NOW,
    )
    interrupted = capture.open_batch(
        store, session_id=STOP_SESSION, transcript_bytes=2_000, offset=900, now=NOW
    )
    capture.close_batch(
        store,
        interrupted,
        outcome=capture.OUTCOME_INTERRUPTED,
        watermark=900,
        transcript_bytes=2_000,
        now=NOW,
    )
    capture.open_batch(
        store, session_id=OTHER_WINDOW, transcript_bytes=10, offset=0, now=NOW
    )
    store.close()

    reopened = Store(path)
    assert capture.watermark(reopened, STOP_SESSION) == 900
    assert capture.watermark(reopened, OTHER_WINDOW) == 0
    assert capture.daily_count(reopened, now=NOW) == 3
    assert capture.daily_count(reopened, now=NOW + timedelta(days=1)) == 0
    last = capture.last_close(reopened, STOP_SESSION)
    assert last is not None and last.outcome == capture.OUTCOME_INTERRUPTED
    [still_open] = capture.open_batches(reopened, now=NOW)
    assert still_open.session_id == OTHER_WINDOW
    assert (still_open.offset, still_open.transcript_bytes) == (0, 10)
