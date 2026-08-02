"""Integration tests for the user-claim gate on memory_update.

`UserClaimGate` (handlers/write.py) exists so a claim ABOUT THE USER filed
as `fact` cannot commit without the pending/veto handshake. It hung off
`memory_write` alone, and `memory_update` replaces a body without
consulting it — so the refusal was one hop wide:

    memory_write(content="the deploy script lives in bin/", category="fact")
    memory_update(id, content="Mattias prefers tabs over spaces.")

landed verbatim the body `memory_write` hard-refuses, in the category whose
label `PendingGate` reads, and the user was never asked. Same laundering
shape the credential gate on this surface already closes for secrets
(tests/test_server_credentials.py), which is why these tests are shaped
like that module's.

What they pin beyond "the gate fires":

- PARITY with the write surface, asserted against `memory_write`'s own
  answer rather than against a hard-coded string, so the two cannot drift
  into disagreeing about the same body.
- The gate reads the category the record will HAVE, not the one the call
  named. `ambient` is gated exactly as `fact` is; `user-inference` is the
  one category it passes, and that is the structural escape — a claim about
  the user belongs in a `user-inference` memory, which only `memory_write`
  can create, staged.
- BODY edits only. A metadata edit on a record whose body already reads as
  a claim has to stay possible, or curating the mis-filed records that
  predate the gate becomes impossible.
- Ordering behind the credential gate, for the reason the write chain
  orders them that way: the refusal event carries body-derived
  `claim_phrases`, so a secret must be refused first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

_CLAIM = "Mattias prefers tabs over spaces."
_CLEAN = "the deploy script lives in bin/deploy.sh"


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    return server, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape.
    """
    return await _mcp_call(server, name, kwargs)


async def _seed_fact(server: Any) -> str:
    """A committed `fact` with a body no gate objects to."""
    res = await _call(server, "memory_write", content=_CLEAN, scopes=["tools"])
    assert res["status"] == "committed"
    return str(res["id"])


# ---------------------------------------------------------------------------
# The hole: write innocuous, update to the claim
# ---------------------------------------------------------------------------


async def test_update_cannot_launder_a_user_claim_into_a_fact(
    server_with_events: tuple[Any, Path],
) -> None:
    """The defect, end to end. Decisive assertion is the body on disk:
    a refusal that still persisted the edit would close nothing."""
    server, memory_dir = server_with_events
    memory_id = await _seed_fact(server)
    res = await _call(server, "memory_update", id=memory_id, content=_CLAIM)
    assert res["status"] == "user_claim_warning"
    assert res["markers"][0]["phrase"] == "Mattias prefers"
    assert res["markers"][0]["sentence"] == _CLAIM
    assert Store(memory_dir).load_one(memory_id).body.strip() == _CLEAN


async def test_update_refuses_with_the_write_surfaces_own_status(
    server_with_events: tuple[Any, Path],
) -> None:
    """Parity, asserted against `memory_write`'s live answer instead of a
    literal. A future rename of the status on one surface and not the
    other is exactly the drift a hard-coded string cannot see."""
    server, _ = server_with_events
    memory_id = await _seed_fact(server)
    updated = await _call(server, "memory_update", id=memory_id, content=_CLAIM)
    written = await _call(
        server, "memory_write", content=_CLAIM, scopes=["learning-style"]
    )
    assert updated["status"] == written["status"] == "user_claim_warning"
    assert updated["markers"] == written["markers"]


async def test_first_person_body_edit_is_gated_too(
    server_with_events: tuple[Any, Path],
) -> None:
    """`_find_user_claims` applies per sentence after apostrophe
    normalization; handing it the raw body kills the `^(?:my|our)` branch.
    The update surface has to inherit that application, not re-do it."""
    server, _ = server_with_events
    memory_id = await _seed_fact(server)
    res = await _call(
        server,
        "memory_update",
        id=memory_id,
        content=(
            "The deploy runs through GitHub Actions.\n"
            "My editor is neovim with a lua config."
        ),
    )
    assert res["status"] == "user_claim_warning"
    assert res["markers"][0]["sentence"] == "My editor is neovim with a lua config."


async def test_ambient_records_are_gated_exactly_as_facts_are(
    server_with_events: tuple[Any, Path],
) -> None:
    """`PendingGate` reads the category label, and `ambient` is as unstaged
    as `fact` — so an `ambient` record is the same laundering vector."""
    server, _ = server_with_events
    created = await _call(
        server,
        "memory_write",
        content="the office is loud on fridays",
        scopes=["tools"],
        category="ambient",
    )
    res = await _call(server, "memory_update", id=created["id"], content=_CLAIM)
    assert res["status"] == "user_claim_warning"


# ---------------------------------------------------------------------------
# The escape: the one category the gate passes
# ---------------------------------------------------------------------------


async def test_a_user_inference_record_accepts_a_claim_body_edit(
    server_with_events: tuple[Any, Path],
) -> None:
    """The acknowledged path that exists today. A claim about the user is
    legal in a `user-inference` memory — the one the user has already
    vetoed or confirmed — so refining that body must stay possible, or the
    gate refuses the only category it wants the claim to live in."""
    server, _ = server_with_events
    staged = await _call(
        server,
        "memory_write",
        content=_CLAIM,
        scopes=["learning-style"],
        category="user-inference",
    )
    assert staged["status"] == "pending"
    confirmed = await _call(
        server, "memory_write_confirm", pending_id=staged["pending_id"]
    )
    assert confirmed["category"] == "user-inference"
    res = await _call(
        server,
        "memory_update",
        id=confirmed["id"],
        content="Mattias prefers tabs over spaces in every editor.",
    )
    assert res["status"] == "committed"


async def test_the_hint_names_a_route_that_exists(
    server_with_events: tuple[Any, Path],
) -> None:
    """A refusal with no legal escape is the failure `_CONFIRM_GATES`
    documents as its reason for excluding the body gates. This one has
    two, and both are on `memory_write` — `category='user-inference'` for
    a real claim, `acknowledge_user_claim` for a subject that is someone
    or something else. The hint has to say so, because retagging this
    record into `user-inference` is refused by the category rule above."""
    server, _ = server_with_events
    memory_id = await _seed_fact(server)
    res = await _call(server, "memory_update", id=memory_id, content=_CLAIM)
    hint = res["hint"]
    assert "memory_write" in hint
    assert "user-inference" in hint
    assert "acknowledge_user_claim" in hint


# ---------------------------------------------------------------------------
# Blast radius: body edits only, and behind the credential gate
# ---------------------------------------------------------------------------


async def test_metadata_only_edits_on_a_mis_filed_record_still_work(
    server_with_events: tuple[Any, Path],
) -> None:
    """Records mis-filed before the gate existed are seeded the way they
    got there — straight through the Store API. Re-scoping one must not
    re-raise a verdict about a body this call does not touch, or curating
    the backlog the gate exists to prevent becomes impossible."""
    server, memory_dir = server_with_events
    seeded = Store(memory_dir).write(content=_CLAIM, scopes=["learning-style"])
    res = await _call(
        server, "memory_update", id=seeded.id, scopes=["learning-style", "tools"]
    )
    assert res["status"] == "committed"
    assert sorted(Store(memory_dir).load_one(seeded.id).scopes) == [
        "learning-style",
        "tools",
    ]


async def test_credential_in_a_claim_shaped_body_reports_the_credential(
    server_with_events: tuple[Any, Path],
) -> None:
    """Ordering, stated as what it protects: the user-claim refusal logs
    body-derived `claim_phrases`, so a secret has to be refused before it
    runs — the same reason `CredentialGate` leads the write chain."""
    server, _ = server_with_events
    memory_id = await _seed_fact(server)
    secret = "".join(("AKIA", "IOSFODNN7EXAMPLE"))
    res = await _call(
        server,
        "memory_update",
        id=memory_id,
        content=f"Mattias prefers the key {secret} for the tutorial.",
    )
    assert res["status"] == "credential_warning"


async def test_the_refusal_event_logs_the_phrase_and_not_the_body(
    server_with_events: tuple[Any, Path],
) -> None:
    """Override-rate telemetry per marker is the entry ticket for widening
    or narrowing the pattern (`UserClaimGate`'s docstring), and it only
    exists if the update surface records the phrase it fired on — the same
    contract `credentials_acknowledged` carries here for secrets."""
    server, memory_dir = server_with_events
    memory_id = await _seed_fact(server)
    await _call(server, "memory_update", id=memory_id, content=_CLAIM)
    refusals = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "update" and e.get("status") == "user_claim_warning"
    ]
    assert refusals, "no user_claim_warning update event recorded"
    assert refusals[-1]["claim_phrases"] == ["Mattias prefers"]
    assert refusals[-1]["category"] == "fact"
    # The phrase, not the sentence: the event is a marker tally, and the
    # rejected body has no business being persisted by the refusal that
    # kept it out of the store.
    raw = "".join(
        p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob(".events*.jsonl"))
    )
    assert _CLAIM not in raw
