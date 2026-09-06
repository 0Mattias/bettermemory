"""Integration tests for the user-claim gate on memory_write.

`PendingGate` triggers on the category LABEL, so before this gate a claim
ABOUT THE USER written as `category='fact'` committed instantly and the
staging flow whose entire purpose is the user's veto never ran. The
content-shape detector that would have caught it (`proposals._PREFERENCE_RE`)
existed, was tested, and was wired ONLY into the Stop-hook extractor.

What these tests pin, beyond "the gate fires":

- BOTH person-shapes. `_PREFERENCE_RE` is first-person only ("I prefer …")
  because it mines the user's own words; a model-authored write is usually
  third-person ("Mattias prefers tabs"). A gate built on `_PREFERENCE_RE`
  alone passes a naive test and misses the dominant real shape.
- The gate's POSITION: before dedup (or a re-issue gets routed to
  memory_update against a mis-filed parent) and before Pending (or
  re-issuing as `user-inference` stops staging).
- The per-sentence, apostrophe-normalized application. Matching the raw
  body instead silently kills the `^(?:my|our)` branch and every curly-quote
  contraction — both fail open, with no test noticing.
- The blast radius. The gate is deliberately OUT of `CONTENT_GATES`:
  `ingest` is a bulk import of the user's own prior auto-memory files and
  `accept_proposal` is a human review decision on a queue whose extractor
  stamps explicit captures ("remember that I prefer X") as `fact` — both
  carry preference prose by construction and neither has a human in the
  loop to flip an acknowledge flag.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.handlers.write import (
    CONTENT_GATES,
    GateContext,
    PendingGate,
    UserClaimGate,
    _find_user_claims,
)
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from ._mcp import input_schema as _input_schema


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

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _write_events(memory_dir: Path) -> list[dict[str, Any]]:
    return [e for e in iter_events(memory_dir) if e["kind"] == "write"]


# ---------------------------------------------------------------------------
# The hole: a claim about the user, filed as `fact`, committed silently
# ---------------------------------------------------------------------------


async def test_third_person_user_claim_as_fact_warns(
    server_with_events: tuple[Any, Path],
) -> None:
    """The shape a MODEL writes. `_PREFERENCE_RE` does not match it —
    a gate that reused that pattern unchanged would commit this."""
    server, memory_dir = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
    )
    assert res["status"] == "user_claim_warning"
    assert res["markers"][0]["phrase"] == "Mattias prefers"
    assert res["markers"][0]["sentence"] == "Mattias prefers tabs over spaces."
    assert "user-inference" in res["hint"]
    assert "acknowledge_user_claim" in res["hint"]
    # Decisive: nothing reached the durable store.
    assert Store(memory_dir).load_all() == []


async def test_first_person_user_claim_as_fact_warns(
    server_with_events: tuple[Any, Path],
) -> None:
    """The shape the Stop hook already detected — and that memory_write
    committed anyway, because the detector was never wired here."""
    server, memory_dir = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="I prefer tabs over spaces.",
        scopes=["learning-style"],
    )
    assert res["status"] == "user_claim_warning"
    assert res["markers"][0]["phrase"] == "I prefer"
    assert Store(memory_dir).load_all() == []


async def test_the_user_subject_as_fact_warns(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="The user avoids rebase on shared branches.",
        scopes=["tools"],
    )
    assert res["status"] == "user_claim_warning"


async def test_ambient_category_is_gated_too(
    server_with_events: tuple[Any, Path],
) -> None:
    """`ambient` commits without a veto exactly like `fact` does, so
    filing a user claim there is the same bypass wearing a different
    label. Only `user-inference` is exempt."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="The user prefers terse code-driven explanations.",
        scopes=["learning-style"],
        category="ambient",
    )
    assert res["status"] == "user_claim_warning"


async def test_ordinary_project_fact_is_untouched(
    server_with_events: tuple[Any, Path],
) -> None:
    """Without this every assertion above would also pass if the gate
    refused unconditionally. The subject-noun shapes that dominate
    project facts ("X runs …", "X needs …") must stay open."""
    server, _ = server_with_events
    for body in (
        "The webapp deploy runs through GitHub Actions.",
        "Postgres runs on port 5433 in the dev compose file.",
        "Docker needs the daemon running before the test suite starts.",
        "The release runbook lives in docs/release.md.",
    ):
        res = await _call(
            server, "memory_write", content=body, scopes=["infrastructure"]
        )
        assert res["status"] == "committed", body


# ---------------------------------------------------------------------------
# The escape hatch, and the override-rate telemetry behind it
# ---------------------------------------------------------------------------


async def test_acknowledged_user_claim_commits(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        acknowledge_user_claim=True,
    )
    assert res["status"] == "committed"
    assert len(Store(memory_dir).load_all()) == 1


async def test_acknowledged_claim_records_the_overridden_phrase(
    server_with_events: tuple[Any, Path],
) -> None:
    """A gate's phrase list is only ever revisited on override-rate
    evidence (the sha-marker retirement at 45/47 is the precedent), so
    the override has to be legible in the event log — the same axis
    `markers_acknowledged` and `credentials_acknowledged` already
    carry."""
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        acknowledge_user_claim=True,
    )
    event = _write_events(memory_dir)[-1]
    assert event["status"] == "committed"
    assert event["user_claims_acknowledged"] == ["Mattias prefers"]


async def test_clean_body_records_empty_user_claims_acknowledged(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="The release runbook lives in docs/release.md.",
        scopes=["infrastructure"],
    )
    assert _write_events(memory_dir)[-1]["user_claims_acknowledged"] == []


async def test_refusal_event_carries_the_matched_phrase_not_the_body(
    server_with_events: tuple[Any, Path],
) -> None:
    """The audit trail names the cause without copying the claim — the
    same discipline the credential gate applies to its `kind`s."""
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
    )
    events = _write_events(memory_dir)
    assert len(events) == 1
    assert events[0]["status"] == "user_claim_warning"
    assert events[0]["claim_phrases"] == ["Mattias prefers"]
    assert events[0]["category"] == "fact"


# ---------------------------------------------------------------------------
# Position in the chain
# ---------------------------------------------------------------------------


async def test_user_inference_category_stages_instead_of_warning(
    server_with_events: tuple[Any, Path],
) -> None:
    """The re-categorize hint has to work. The gate sits BEFORE
    PendingGate, so an exemption that skipped the gate by rejecting
    early would strand the caller in a loop: warned as `fact`, warned
    again as `user-inference`."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert res["status"] == "pending"
    assert res["pending_reason"] == "user-inference"


async def test_user_inference_reason_wins_over_global_confirmation(
    memory_dir: Path,
) -> None:
    """Ordering INSIDE PendingGate: when `category='user-inference'`
    and `require_write_confirmation=true` both apply, the category's
    reason must win. The hint dispatched on `pending_reason` is the
    only enforcement of the ask-the-user veto — a `config` reason
    hands the model the generic self-confirm hint, so the stricter
    global setting would silently drop the ceremony the category
    structurally promises."""
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert res["status"] == "pending"
    assert res["pending_reason"] == "user-inference"
    assert "ask the user" in res["hint"].lower()
    # The event log carries the same attribution the response does.
    event = _write_events(memory_dir)[-1]
    assert event["pending_reason"] == "user-inference"
    assert event["category"] == "user-inference"


async def test_user_claim_beats_duplicate_on_a_mis_filed_parent(
    server_with_events: tuple[Any, Path],
) -> None:
    """Position-before-dedup, stated as the failure it prevents.

    A mis-filed parent already sits in the store (seeded through the
    Store API, which is how it got there before this gate existed). With
    the gate after dedup the caller gets `duplicate`, whose hint routes
    them to memory_update ON THAT PARENT — the claim is edited into the
    wrong category forever and the user is never asked. The user-claim
    verdict has to win."""
    server, memory_dir = server_with_events
    Store(memory_dir).write(
        content="Mattias prefers tabs over spaces in every editor.",
        scopes=["learning-style"],
    )
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces in every editor.",
        scopes=["learning-style"],
    )
    assert res["status"] == "user_claim_warning"


async def test_transient_marker_still_reported_first(
    server_with_events: tuple[Any, Path],
) -> None:
    """The gate slots in AFTER TransientGate: a body that is both
    transient and user-shaped is unsalvageable as written, and the
    durability fix is the more actionable one."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs, currently.",
        scopes=["learning-style"],
    )
    assert res["status"] == "transient_warning"


# ---------------------------------------------------------------------------
# How the pattern is applied — the silent-degradation surface
# ---------------------------------------------------------------------------


async def test_possessive_claim_matches_only_per_sentence(
    server_with_events: tuple[Any, Path],
) -> None:
    """`_PREFERENCE_RE`'s `^(?:my|our)` branch anchors to the START of
    whatever string it is handed. Hand it the whole body and this
    commits — the gate fails open with every test above still green."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=(
            "The deploy runs through GitHub Actions.\n"
            "My editor is neovim with a lua config."
        ),
        scopes=["tools"],
    )
    assert res["status"] == "user_claim_warning"
    assert res["markers"][0]["sentence"] == "My editor is neovim with a lua config."


async def test_curly_apostrophe_body_still_matches(
    server_with_events: tuple[Any, Path],
) -> None:
    """macOS and iOS substitute smart quotes by default and every
    contraction branch of the shared pattern is written against the
    ASCII apostrophe — skip the normalization and "I’m using …" walks
    straight through."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="I’m using ripgrep for every file search in this repo.",
        scopes=["tools"],
    )
    assert res["status"] == "user_claim_warning"


def test_short_claim_clears_the_extractor_length_floor() -> None:
    """The proposals extractor drops candidates under 30 chars / 6
    tokens to keep a REVIEW QUEUE quiet. Reusing that floor here would
    exempt the canonical case: "Mattias prefers tabs" is 20 chars and
    3 tokens."""
    body = "Mattias prefers tabs"
    assert len(body) < 30 and len(body.split()) < 6
    assert [h.phrase for h in _find_user_claims(body)] == ["Mattias prefers"]


# ---------------------------------------------------------------------------
# Blast radius: the gate is NOT a content gate
# ---------------------------------------------------------------------------


def test_user_claim_gate_is_excluded_from_content_gates() -> None:
    """`CONTENT_GATES` is derived by EXCLUSION, so a gate added to the
    chain joins it automatically — and the two batch callers that use it
    (`apply_ingest_plan`, and `accept_proposal` once it converts) carry
    preference prose by construction with every acknowledge flag False.
    Inheriting this gate turns both into hard refusals with no override
    reachable."""
    kinds = [type(g).__name__ for g in CONTENT_GATES]
    assert "UserClaimGate" not in kinds
    assert "PendingGate" not in kinds
    assert not any(isinstance(g, (UserClaimGate, PendingGate)) for g in CONTENT_GATES)


def test_gate_context_user_claim_flag_defaults_false() -> None:
    """`ingest._gate_context` passes every field by keyword with no
    `**kwargs` slack, so a field added without a default breaks that
    caller at construction. Built here exactly as ingest builds it —
    with no user-claim argument at all."""
    gc = GateContext(
        payload={"content": "x", "scopes": ["tools"]},
        force=False,
        acknowledge_transient=False,
        acknowledge_scope_mismatch=False,
        acknowledge_ungrounded=False,
        acknowledge_credential=False,
        groundedness_check=False,
        source_transcript=None,
    )
    assert gc.acknowledge_user_claim is False
    assert gc.user_claim_hits == []


def test_ingest_still_imports_a_first_person_preference_file(
    tmp_path: Path,
) -> None:
    """End-to-end proof for the exclusion above: auto-memory files ARE
    the user's own words, so first-person preference prose is the norm
    there, not a model asserting a fresh claim. This row must land."""
    from bettermemory.ingest import apply_ingest_plan, compute_ingest_plan

    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "style.md").write_text(
        "\n".join(
            [
                "---",
                "name: style",
                "description: editor preferences",
                "---",
                "",
                "I prefer tabs over spaces in every editor I use.",
                "",
            ]
        )
    )
    store = Store(tmp_path / "store")
    plan = compute_ingest_plan(
        source_root,
        existing_memories=store.load_all(),
        existing_tombstones=store.load_tombstones(),
    )
    apply_ingest_plan(plan, store)
    [row] = plan.rows
    assert row.action == "write", row.reason
    assert row.written_id is not None
    assert len(store.load_all()) == 1


async def test_accepting_a_preference_proposal_still_writes(
    server_with_events: tuple[Any, Path],
) -> None:
    """The proposals extractor stamps explicit captures ("remember that
    I prefer X") as `fact` BY DESIGN, and accepting one is a human
    review decision — the acceptance must not be refused for having the
    shape the queue exists to carry. Guards the F7 conversion too: it
    swaps the hand-rolled scan for `CONTENT_GATES`, which is exactly the
    tuple this gate stays out of."""
    from bettermemory.proposals import Proposal, ProposalQueue

    server, memory_dir = server_with_events
    ProposalQueue(memory_dir).append(
        [
            Proposal(
                id="p1",
                body="I prefer terse code-driven explanations over long prose.",
                source_excerpt="I prefer terse code-driven explanations over prose.",
                suggested_category="fact",
                created="2026-01-01T00:00:00Z",
            )
        ]
    )
    res = await _call(
        server,
        "memory_proposals",
        action="accept",
        proposal_id="p1",
        scopes=["learning-style"],
    )
    assert res["status"] == "accepted"
    assert len(Store(memory_dir).load_all()) == 1


async def test_episode_promotion_of_a_user_claim_is_refused_for_free(
    server_with_events: tuple[Any, Path],
) -> None:
    """`episode_promote` routes through `memory_write` with every
    acknowledge flag at its default, which is the whole point of the
    escape hatches living on `GateContext` rather than inside the gates:
    an unattended caller inherits the refusal without carrying its own
    copy of the check. The source episode survives, so the caller can
    re-promote as `user-inference`."""
    server, _ = server_with_events
    ep = await _call(
        server,
        "episode_write",
        body="Reviewed the formatter config with Mattias this afternoon.",
        takeaway="Mattias prefers tabs over spaces.",
    )
    res = await _call(
        server,
        "episode_promote",
        episode_id=ep["id"],
        scopes=["learning-style"],
    )
    assert res["status"] == "user_claim_warning"
    assert res["promoted_from_episode_id"] == ep["id"]
    listed = await _call(server, "episode_search")
    listed = listed.get("result", listed) if isinstance(listed, dict) else listed
    assert any(e["id"] == ep["id"] for e in listed)


# ---------------------------------------------------------------------------
# The wire surface
# ---------------------------------------------------------------------------


async def test_acknowledge_user_claim_is_exposed_on_the_mcp_schema(
    server_with_events: tuple[Any, Path],
) -> None:
    """The `_handlers.py` facade signature IS the served schema. Add the
    parameter to the handler only and the escape hatch exists in Python
    and nowhere on the wire — every model that hits the refusal is stuck
    with no way to override."""
    server, _ = server_with_events
    tools = {t.name: t for t in await server.list_tools()}
    props = _input_schema(tools["memory_write"])["properties"]
    assert "acknowledge_user_claim" in props
    assert props["acknowledge_user_claim"].get("default") is False


async def test_status_vocabulary_is_documented(
    server_with_events: tuple[Any, Path],
) -> None:
    """A refusal status the model has never read about is a dead end:
    the DESC is where it learns the re-categorize move exists."""
    server, _ = server_with_events
    tools = {t.name: t for t in await server.list_tools()}
    desc = tools["memory_write"].description or ""
    assert "user_claim_warning" in desc
    assert "acknowledge_user_claim" in desc


# ---------------------------------------------------------------------------
# Quotation exempts the first-person leg only
# ---------------------------------------------------------------------------


def test_quoted_owner_words_do_not_read_as_a_user_claim() -> None:
    """`_PREFERENCE_RE` is a transcript miner — first person there means
    the user because the user typed it. In a memory BODY the author is the
    assistant, so first person is either its own voice or a transcription.
    Staging a verbatim owner ruling as `user-inference` would ask the user
    to confirm that they said what they are quoted saying."""
    body = (
        "(2) 2026-08-11 canonical correction: \"I never said 'no neural "
        "weights', I said no sloppy bullshit. You can add neural weights "
        'as long as we built the model from scratch" — from-scratch neural '
        "legal, third-party pretrained weights still banned."
    )
    assert _find_user_claims(body) == []


def test_the_third_person_leg_still_fires_inside_a_quotation() -> None:
    """`_USER_CLAIM_RE` reads the shape a MODEL writes when it files a
    claim of its own. It has no quoted fires in the measured store, and
    narrowing an unfired leg on no evidence is how a gate stops working."""
    body = 'The retro notes said "the user prefers tabs over spaces" verbatim.'
    assert [hit.phrase for hit in _find_user_claims(body)] == ["the user prefers"]


def test_a_quotation_does_not_silence_a_later_first_person_assertion() -> None:
    body = 'He said "i like dark mode". I always use dark mode as well.'
    assert [hit.phrase for hit in _find_user_claims(body)] == ["I always"]


def test_quotation_exemption_survives_a_hard_wrapped_quote() -> None:
    """`_HARD_WRAP_RE` rejoins soft-wrapped prose before spans are
    measured, so a quotation broken across a wrap is still one span."""
    body = 'The owner wrote: "i want this to store our\ntraining data etc" verbatim.'
    assert _find_user_claims(body) == []


def test_quotation_exemption_survives_a_list_prefix() -> None:
    """Offsets are threaded through the bullet strip, so a quoted claim in
    a list item is still located inside its span."""
    body = '- 2026-08-19 owner ruling: "i like verifiable memory" and nothing else.'
    assert _find_user_claims(body) == []


def test_unquoted_first_person_assistant_voice_still_blocks() -> None:
    """Named so it is not mistaken for solved: the residue quotation does
    not clear is unquoted first person in the ASSISTANT's voice, where the
    pronoun heads a relative clause rather than a self-report. Still a
    false positive, still blocking — separating it needs a clause-position
    rule and there is not enough evidence to tune one."""
    body = "A memory about my own error is the one I never think to query for."
    assert [hit.phrase for hit in _find_user_claims(body)] == ["I never"]
