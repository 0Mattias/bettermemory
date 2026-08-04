"""Integration tests for the truncation gate on memory_update.

`looks_truncated` (models.py) shipped as DETECTION first: `doctor`'s
`memory_body_completeness` check reports bodies whose last non-whitespace
character is not sentence- or structure-terminal. That is reporting after
the fact, and the report names a loss it cannot undo — the store keeps no
older copy, so by the time the check speaks the tail is gone. One memory in
the maintainer's store sat truncated mid-word for ten days with every check
green.

`memory_update` is the one place both bodies are in hand, so it is the one
place the loss is preventable. The gate is that predicate moved there:

    memory_update(id, content="<a rewrite that arrived cut off>")

returns `truncation_warning` and persists nothing, with
`acknowledge_truncation=True` as the escape.

What these pin beyond "the gate fires":

- THE SHRINK CONJUNCT, which is the whole reason this can be a gate at all.
  `looks_truncated` alone is 0.4% false positive on a 234-record store —
  fine for a report the operator reads, ruinous for a gate, because it
  would refuse every edit to a body that legitimately ends on a bare
  identifier or a list item, forever, INCLUDING edits that only grew it.
  Two tests below are the negative controls for that: a growing edit that
  ends mid-sentence commits, and a shrinking edit that ends on a terminal
  character commits. Delete the `len(...) < len(...)` conjunct and both go
  red; delete `looks_truncated` and the second one does. Neither would be
  caught by testing the refusal alone.
- BODY edits only. A metadata edit on a record whose body already reads
  truncated has to stay possible, or the records that predate the gate —
  the exact ones `doctor` is pointing at — become uncurateable.
- ORDERING behind the credential gate, for the reason the write chain
  orders them that way: a secret is refused before any other gate records
  body-derived data in the event log.
- THE WIRE. `acknowledge_truncation` has to be on the served schema, which
  is built from the `_handlers.py` facade signature — a handler-only
  parameter is silently dropped at call time, which is the failure mode
  `acknowledge_user_claim` actually hit once.
- REDACTION. The refusal event carries LENGTHS, never body text. A
  truncated body is exactly as likely to carry a secret as any other, and
  a gate that logs what it refused would be a worse leak than the one the
  credential gate closes.
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

from ._mcp import call_tool as _mcp_call, input_schema as _input_schema

# The seed is long, ends on a period, and says something whole. Every case
# below is an edit away from this one body, so the only variables are
# LENGTH and FINAL CHARACTER.
_WHOLE = (
    "The release runbook lives in docs/release.md. It bumps seven version "
    "fields across six files, and the tag is cut only after the full CI "
    "matrix reports green on the release commit itself."
)
# Shorter than `_WHOLE` and ends mid-word: the shape a body cut off in
# transit actually has, and the shape the ten-day incident had.
_CUT = "The release runbook lives in docs/release.md. It bumps seven version fi"
# Shorter than `_WHOLE` and ends on a period. This is the single most common
# update shape on the dogfood store — a condensing rewrite — and the gate
# must never see it.
_CONDENSED = "The release runbook lives in docs/release.md."
# LONGER than `_WHOLE` and ends on a bare identifier, so `looks_truncated`
# says yes while the edit plainly lost nothing. This is the 0.4% false
# positive, and the shrink conjunct is what makes it commit.
_GREW_ENDING_BARE = (
    _WHOLE + " The seven fields are listed in tests/test_plugin.py and "
    "tests/test_changelog.py"
)


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
    """Invoke a tool and return its structured payload."""
    return await _mcp_call(server, name, kwargs)


async def _seed(server: Any, body: str = _WHOLE) -> str:
    res = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert res["status"] == "committed"
    return str(res["id"])


async def _body_on_disk(server: Any, memory_id: str) -> str:
    res = await _call(server, "memory_show", id=memory_id)
    return str(res["body"])


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


async def test_a_shrinking_edit_that_ends_mid_sentence_is_refused(
    server_with_events: tuple[Any, Path],
) -> None:
    """The defect, end to end. The decisive assertion is the body on disk:
    a refusal that still persisted the edit would close nothing, and the
    store has no older copy to fall back on."""
    server, _ = server_with_events
    memory_id = await _seed(server)

    res = await _call(server, "memory_update", id=memory_id, content=_CUT)

    assert res["status"] == "truncation_warning"
    assert res["previous_length"] == len(_WHOLE)
    assert res["new_length"] == len(_CUT)
    assert res["ends_with"].endswith("version fi")
    assert _WHOLE in await _body_on_disk(server, memory_id)


async def test_the_refusal_event_carries_lengths_and_never_the_body(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    memory_id = await _seed(server)
    await _call(server, "memory_update", id=memory_id, content=_CUT)

    refusals = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "update" and e.get("status") == "truncation_warning"
    ]
    assert refusals, "no truncation_warning update event recorded"
    assert refusals[-1]["previous_length"] == len(_WHOLE)
    assert refusals[-1]["new_length"] == len(_CUT)

    raw = "".join(
        p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob(".events*.jsonl"))
    )
    assert _CUT not in raw, "the refused body reached the event log"


# ---------------------------------------------------------------------------
# The negative controls — why this predicate is tolerable as a gate
# ---------------------------------------------------------------------------


async def test_a_growing_edit_that_ends_mid_sentence_commits(
    server_with_events: tuple[Any, Path],
) -> None:
    """The shrink conjunct's reason for existing.

    `looks_truncated(_GREW_ENDING_BARE)` is True — it ends on a bare
    filename. Without the length comparison this edit is refused, and so is
    every future edit to any body that legitimately ends on an identifier or
    a list item. That is the 0.4% false-positive rate turned into a
    permanent refusal, which is precisely the trade the roadmap entry
    rejected for a gate.
    """
    server, _ = server_with_events
    memory_id = await _seed(server)

    res = await _call(server, "memory_update", id=memory_id, content=_GREW_ENDING_BARE)

    assert res["status"] == "committed", res
    assert "tests/test_changelog.py" in await _body_on_disk(server, memory_id)


async def test_a_shrinking_edit_that_ends_on_a_terminal_commits(
    server_with_events: tuple[Any, Path],
) -> None:
    """Condensing is the most common update shape on the dogfood store, and
    the gate must be invisible to it. This is what rules out the rejected
    ">30% shorter" variant, which would refuse exactly this."""
    server, _ = server_with_events
    memory_id = await _seed(server)

    res = await _call(server, "memory_update", id=memory_id, content=_CONDENSED)

    assert res["status"] == "committed", res
    assert (await _body_on_disk(server, memory_id)).strip() == _CONDENSED


async def test_a_metadata_only_edit_on_a_truncated_record_is_untouched(
    server_with_events: tuple[Any, Path],
) -> None:
    """The records this gate exists for are already IN the store — `doctor`
    is pointing at one. Re-scoping or re-tagging them has to stay possible,
    or the gate makes its own backlog uncurateable."""
    server, _ = server_with_events
    memory_id = await _seed(server, body=_CUT)

    res = await _call(server, "memory_update", id=memory_id, scopes=["tools", "infra"])

    assert res["status"] == "committed", res


# ---------------------------------------------------------------------------
# The override, and the wire it has to arrive on
# ---------------------------------------------------------------------------


async def test_the_override_commits_and_the_event_records_it(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    memory_id = await _seed(server)

    res = await _call(
        server,
        "memory_update",
        id=memory_id,
        content=_CUT,
        acknowledge_truncation=True,
    )

    assert res["status"] == "committed", res
    assert (await _body_on_disk(server, memory_id)).strip() == _CUT

    commits = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "update" and e.get("status") is None
    ]
    assert commits, "no committed update event recorded"
    assert commits[-1]["truncation_acknowledged"] is True


async def test_the_field_is_present_on_unacknowledged_commits_too(
    server_with_events: tuple[Any, Path],
) -> None:
    """The override RATE is the only evidence that would ever reopen this
    predicate, and a rate needs a denominator. A field that appears only on
    the acknowledged path makes `grep truncation_acknowledged` count
    numerators."""
    server, memory_dir = server_with_events
    memory_id = await _seed(server)

    await _call(server, "memory_update", id=memory_id, content=_CONDENSED)

    commits = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "update" and e.get("status") is None
    ]
    assert commits[-1]["truncation_acknowledged"] is False


async def test_the_override_is_served_on_the_wire_defaulting_to_off(
    server_with_events: tuple[Any, Path],
) -> None:
    """The served schema is built from the `_handlers.py` facade signature,
    so a parameter that reaches only the handler is dropped at call time and
    the refusal comes back anyway with nothing saying the flag did nothing.
    `acknowledge_user_claim` shipped that way once."""
    server, _ = server_with_events
    tools = {t.name: t for t in await server.list_tools()}
    schema = _input_schema(tools["memory_update"])

    props = schema["properties"]
    assert "acknowledge_truncation" in props, (
        "memory_update does not serve `acknowledge_truncation`, so a caller "
        "passing it is silently ignored and gets the refusal anyway. Served: "
        f"{sorted(props)}"
    )
    assert props["acknowledge_truncation"]["type"] == "boolean"
    # `default: False` is half the contract: a flag that defaulted to True
    # would silently disable the gate for every caller.
    assert props["acknowledge_truncation"].get("default") is False
    assert "acknowledge_truncation" not in schema.get("required", [])


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


async def test_the_credential_gate_refuses_first(
    server_with_events: tuple[Any, Path],
) -> None:
    """A body that trips BOTH gates must come back `credential_warning`.

    The write chain orders them that way so a secret is refused before any
    other gate records body-derived data in the event log — and the
    truncation refusal records lengths of exactly the body holding the
    secret.
    """
    server, _ = server_with_events
    memory_id = await _seed(server)
    both = "AKIAIOSFODNN7EXAMPLE is the key and the rest of this was cut off mid-wor"
    assert len(both) < len(_WHOLE)

    res = await _call(server, "memory_update", id=memory_id, content=both)

    assert res["status"] == "credential_warning", res
