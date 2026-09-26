"""Integration tests for the user-claim gate on memory_write.

The category LABEL is the only thing that marks a stored claim about the
user as an inference, so before this gate a claim ABOUT THE USER written
as `category='fact'` read back as an established fact, indistinguishable
from the project facts beside it. The content-shape detector that would
have caught it (`proposals._PREFERENCE_RE`) existed, was tested, and was
wired ONLY into the Stop-hook extractor. The gate fixes the label only:
`user-inference` commits exactly as `fact` does, and the user never sees
the refusal.

What these tests pin, beyond "the gate fires":

- BOTH person-shapes. `_PREFERENCE_RE` is first-person only ("I prefer …")
  because it mines the user's own words; a model-authored write is usually
  third-person ("Mattias prefers tabs"). A gate built on `_PREFERENCE_RE`
  alone passes a naive test and misses the dominant real shape.
- The gate's POSITION: before dedup (or a re-issue gets routed to
  memory_update against a mis-filed parent).
- The per-sentence, apostrophe-normalized application. Matching the raw
  body instead silently kills the `^(?:my|our)` branch and every curly-quote
  contraction — both fail open, with no test noticing.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.handlers.write import GateContext, _find_user_claims
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store
from ._mcp import input_schema as _input_schema


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Store]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    store = Store(memory_dir)
    rec = Recorder(store=store, session_id=state.session_id)
    server = build_server(
        config=cfg,
        store=store,
        state=state,
        recorder=rec,
    )
    return server, store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _write_events(store: Store) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e["kind"] == "write"]


# ---------------------------------------------------------------------------
# The hole: a claim about the user, filed as `fact`, committed silently
# ---------------------------------------------------------------------------


async def test_third_person_user_claim_as_fact_warns(
    server_with_events: tuple[Any, Store],
) -> None:
    """The shape a MODEL writes. `_PREFERENCE_RE` does not match it —
    a gate that reused that pattern unchanged would commit this."""
    server, store = server_with_events
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
    assert store.load_all() == []


async def test_first_person_user_claim_as_fact_warns(
    server_with_events: tuple[Any, Store],
) -> None:
    """The shape the Stop hook already detected — and that memory_write
    committed anyway, because the detector was never wired here."""
    server, store = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="I prefer tabs over spaces.",
        scopes=["learning-style"],
    )
    assert res["status"] == "user_claim_warning"
    assert res["markers"][0]["phrase"] == "I prefer"
    assert store.load_all() == []


async def test_the_user_subject_as_fact_warns(
    server_with_events: tuple[Any, Store],
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
    server_with_events: tuple[Any, Store],
) -> None:
    """`ambient` reads back as unlabelled context exactly like `fact`
    reads back as established, so filing a user claim there is the same
    mislabel wearing a different name. Only `user-inference` is exempt."""
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
    server_with_events: tuple[Any, Store],
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
    server_with_events: tuple[Any, Store],
) -> None:
    server, store = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        acknowledge_user_claim=True,
    )
    assert res["status"] == "committed"
    assert len(store.load_all()) == 1


async def test_acknowledged_claim_records_the_overridden_phrase(
    server_with_events: tuple[Any, Store],
) -> None:
    """A gate's phrase list is only ever revisited on override-rate
    evidence (the sha-marker retirement at 45/47 is the precedent), so
    the override has to be legible in the event log — the same axis
    `markers_acknowledged` and `credentials_acknowledged` already
    carry."""
    server, store = server_with_events
    await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        acknowledge_user_claim=True,
    )
    event = _write_events(store)[-1]
    assert event["status"] == "committed"
    assert event["user_claims_acknowledged"] == ["Mattias prefers"]


async def test_clean_body_records_empty_user_claims_acknowledged(
    server_with_events: tuple[Any, Store],
) -> None:
    server, store = server_with_events
    await _call(
        server,
        "memory_write",
        content="The release runbook lives in docs/release.md.",
        scopes=["infrastructure"],
    )
    assert _write_events(store)[-1]["user_claims_acknowledged"] == []


async def test_refusal_event_carries_the_matched_phrase_not_the_body(
    server_with_events: tuple[Any, Store],
) -> None:
    """The audit trail names the cause without copying the claim — the
    same discipline the credential gate applies to its `kind`s."""
    server, store = server_with_events
    await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
    )
    events = _write_events(store)
    assert len(events) == 1
    assert events[0]["status"] == "user_claim_warning"
    assert events[0]["claim_phrases"] == ["Mattias prefers"]
    assert events[0]["category"] == "fact"


# ---------------------------------------------------------------------------
# Position in the chain
# ---------------------------------------------------------------------------


async def test_the_relabelled_reissue_commits(
    server_with_events: tuple[Any, Store],
) -> None:
    """The re-categorize hint has to work, and in one call: an exemption
    that skipped the gate by rejecting early would strand the caller in a
    loop (warned as `fact`, warned again as `user-inference`), and a
    re-issue that staged would put the confirmation round trip back in
    front of the user. The claim lands, with its label, on the re-issue."""
    server, store = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Mattias prefers tabs over spaces.",
        scopes=["learning-style"],
        category="user-inference",
    )
    assert res["status"] == "committed"
    assert res["category"] == "user-inference"
    [stored] = store.load_all()
    assert stored.category is not None
    assert stored.category.value == "user-inference"
    event = _write_events(store)[-1]
    assert event["status"] == "committed"
    assert event["category"] == "user-inference"


async def test_user_claim_beats_duplicate_on_a_mis_filed_parent(
    server_with_events: tuple[Any, Store],
) -> None:
    """Position-before-dedup, stated as the failure it prevents.

    A mis-filed parent already sits in the store (seeded through the
    Store API, which is how it got there before this gate existed). With
    the gate after dedup the caller gets `duplicate`, whose hint routes
    them to memory_update ON THAT PARENT — the claim is edited into the
    wrong category forever, and memory_update cannot relabel it. The
    user-claim verdict has to win."""
    server, store = server_with_events
    store.write(
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
    server_with_events: tuple[Any, Store],
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
    server_with_events: tuple[Any, Store],
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
    server_with_events: tuple[Any, Store],
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
# Blast radius: the flag is an escape hatch on the context, off by default
# ---------------------------------------------------------------------------


def test_gate_context_user_claim_flag_defaults_false() -> None:
    """A caller that passes every field by keyword with no `**kwargs`
    slack breaks at construction the moment a field is added without a
    default. Built here with no user-claim argument at all."""
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


# ---------------------------------------------------------------------------
# The wire surface
# ---------------------------------------------------------------------------


async def test_acknowledge_user_claim_is_exposed_on_the_mcp_schema(
    server_with_events: tuple[Any, Store],
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


# ---------------------------------------------------------------------------
# Quotation exempts the first-person leg only
# ---------------------------------------------------------------------------


def test_quoted_owner_words_do_not_read_as_a_user_claim() -> None:
    """`_PREFERENCE_RE` is a transcript miner — first person there means
    the user because the user typed it. In a memory BODY the author is the
    assistant, so first person is either its own voice or a transcription.
    Filing a verbatim owner ruling as `user-inference` would label what
    the user is quoted saying as something inferred about them."""
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
