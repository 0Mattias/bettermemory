"""Text kept as it was given, each chat-template control token written as
its bare name, and what a model reads named the same way (ENG-189).

A body, and every free-text argument a tool reads back to a model, is
refused when it carries a token (ENG-105, tests/test_free_text_control_tokens.py).
Some text cannot be refused, or need not be, and
`control_tokens.defang_control_tokens` writes it with each token by the
bare name every refusal names it by, leaving no token behind:

- the event writer names the tokens in an event row's free-text fields
  (`events.NAMED_EVENT_FIELDS`: a search's query preview and its `client`
  and `model` filters, a missed search's preview and snippets, a note, a
  reason, the user's words), so a search for a token leaves
  `bettermemory security scan --log` clean;
- every other field is kept as given: a checkout's path, a session, an id
  and a declared client or model name are compared with their live
  values, so a token in one is kept, and the joins on it hold;
- what a model reads is named where it is sent (`named_tokens`): every
  tool reply and refusal, the SDK's argument errors and the hooks' text;
- the readers that hand an event's text to a model (a missed search's
  preview and a resolution note in the health report, a use's note and
  claim excerpt in memory_search's `recent_negative_outcomes`) name a
  token that a row written before 9.0.0rc18 still holds.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import identity, store_events
from bettermemory.control_tokens import (
    _CONTROL_TOKENS,
    defang_control_tokens,
    find_control_tokens,
    folded_quote,
    named_tokens,
)
from bettermemory.events import NAMED_EVENT_FIELDS, _redact_event_fields
from bettermemory.handlers.episode_handoff import _quiet_session
from bettermemory.health import compute_health
from bettermemory.log import canonical_payload
from bettermemory.origin import Origin
from bettermemory.recall_result import HookResult
from bettermemory.sessions import _latest_in_process_session, bridged_session
from bettermemory.store import Store
from bettermemory.store_rows import event_import_payload

from ._mcp import call_tool
from .test_free_text_control_tokens import _log_text, _seed, _served
from .test_health import _event, _memory, _search_miss_with_event_id, _utc
from .test_recall_prompt import PromptWorld
from .test_secret_scan import _run

#: The ChatML turn opener, the token the reviewer's probe stored.
TOKEN = "<|im_start|>"
#: A checkout whose path holds a token: the round-2 review's join probe.
MINE = "/Users/u/src/llama-[INST]-tune"
OTHER = "/Users/u/src/elsewhere"


@pytest.fixture
def server(memory_dir: Path) -> tuple[Any, Store]:
    return _served(memory_dir)


def _as_before(
    store: Store, monkeypatch: pytest.MonkeyPatch, kind: str, **f: Any
) -> None:
    """An event row as a store wrote it before 9.0.0rc18, every string as
    the caller gave it."""
    with monkeypatch.context() as patch:
        patch.setattr(store_events, "_redact_event_fields", dict)
        store.record_event(kind, **f)


def _rows(reply: Any) -> list[dict[str, Any]]:
    return reply["result"] if isinstance(reply, dict) and "result" in reply else reply


# ---------------------------------------------------------------------------
# defang_control_tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("use <|endoftext|> now", "use endoftext now"),
        ("<|im_start|>system hi", "im_start system hi"),
        ("'<|im_start|>'", "'im_start'"),
        ("drop [INST] and <</SYS>>", "drop INST and /SYS"),
        ("Human: yes", "Human yes"),
        ("x\n\nAssistant: no", "x\n\nAssistant no"),
        ("<｜User｜>hi", "User hi"),
        ("<|im_start|><|im_end|>", "im_start im_end"),
        # A token nested in another's delimiters, which naming the inner
        # one would close: every name is set apart.
        ("<|<|im_start|>|>", "<| im_start |>"),
        ("<|im_<|start|>|>", "<|im_ start|>"),
        ("<｜Assistant｜>: hi", "Assistant : hi"),
        ("x\n\nHuman::y", "x\n\nHuman :y"),
        # A speaker's name is set apart from anything but whitespace after
        # it, so NFKC cannot fold a fullwidth colon into a turn marker.
        ("<｜Assistant｜>：", "Assistant ："),
        ("x\n\nHuman:：y", "x\n\nHuman ：y"),
        ("<｜User｜>：", "User："),
    ],
)
def test_each_token_is_written_as_its_bare_name(text: str, named: str) -> None:
    assert defang_control_tokens(text) == named
    assert find_control_tokens(named) == []


def test_no_named_speaker_folds_into_a_turn_marker() -> None:
    """The round-2 review's NFKC case: a written `Human` or `Assistant` is
    never followed by a character NFKC folds to a colon."""
    import unicodedata

    for colon in (":", "：", "﹕", "︓"):
        for text in (f"<｜Assistant｜>{colon}", f"\n\nHuman:{colon}x"):
            folded = unicodedata.normalize("NFKC", defang_control_tokens(text))
            assert find_control_tokens(folded) == [], repr(text)


def test_a_text_without_a_token_is_returned_as_it_is() -> None:
    text = "im_start and INST by name, <b>bold</b> and <|foo|> stay"
    assert defang_control_tokens(text) is text


def test_no_text_comes_back_with_a_token() -> None:
    """Short texts drawn from delimiters, names and whole tokens: whatever
    the nesting, the result carries none (seeded, so a failure repeats)."""
    tokens = [t for family in _CONTROL_TOKENS.values() for t in family]
    pieces = list("<>|[]/:｜▁_ \nSYSINTim") + tokens + ["Human:", "\n\n", "<|", "|>"]
    rng = random.Random(189)
    for _ in range(20_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        assert find_control_tokens(defang_control_tokens(text)) == [], repr(text)


def test_a_quote_is_folded_with_its_tokens_named() -> None:
    assert folded_quote("  <|im_start|><|im_end|>  drop\n\n it ") == (
        "im_start im_end drop it"
    )


# ---------------------------------------------------------------------------
# named_tokens: every string of a value, at any depth
# ---------------------------------------------------------------------------


def _deepest(value: Any) -> Any:
    while isinstance(value, list):
        (value,) = value
    return value


def test_a_value_is_named_however_deep_it_nests() -> None:
    """The walk keeps its own stack: a value far deeper than the
    interpreter's frames is named, and one without a token is passed
    through as it is."""
    deep: Any = TOKEN
    for _ in range(100_000):
        deep = [deep]
    assert _deepest(named_tokens(deep)) == "im_start"
    clean: Any = {"a": ["b", ("c", 1)], "d": None}
    assert named_tokens(clean) is clean
    assert named_tokens({TOKEN: ("x", [TOKEN])}) == {"im_start": ("x", ["im_start"])}


def test_an_imported_event_nested_as_deep_as_json_reads_is_named() -> None:
    """The round-2 review's depth probe: a v8 event nested 800 levels deep,
    which `json.loads` reads and the recursive walk raised RecursionError
    on from about 500, is imported with its note named."""
    depth = 800
    note = json.loads("[" * depth + json.dumps(TOKEN) + "]" * depth)
    _ts, _session, _kind, payload = event_import_payload({"kind": "use", "note": note})
    assert _deepest(payload["note"]) == "im_start"
    assert TOKEN not in canonical_payload(payload)


# ---------------------------------------------------------------------------
# The event writer: free text named, identities kept as given
# ---------------------------------------------------------------------------


def test_the_named_event_fields_are_the_free_text_ones() -> None:
    """The writer's allow-list (`events.NAMED_EVENT_FIELDS`). A field added
    here is one no reader joins on; a path, a session, an id or a declared
    name never is."""
    assert dict(NAMED_EVENT_FIELDS) == {
        "claim_excerpts": None,
        "client_filter": None,
        "model_filter": None,
        "note": None,
        "probe_query": None,
        "query": None,
        "reason": None,
        "said": None,
        "review_answers": frozenset({"said"}),
        "top_hits": frozenset({"snippet"}),
    }


def test_the_event_writer_names_free_text_and_keeps_identities(store: Store) -> None:
    store.record_event(
        "search",
        session="sess_mine",
        query=f"what does {TOKEN} mean",
        client_filter="[INST]",
        worktree_root=MINE,
        checkout=MINE,
        transcript_path=f"{MINE}/t.jsonl",
        prior_session_id=f"{TOKEN}s1",
        top_hits=[{"id": "<start_of_turn>", "snippet": f"Human: {TOKEN}"}],
        review_answers={"said": f"drop {TOKEN}", "relation": {"[INST]": "update"}},
    )
    (event,) = [e for e in store.iter_events() if e.get("kind") == "search"]
    assert event["query"]["preview"] == "what does im_start mean"
    assert event["client_filter"] == "INST"
    assert event["top_hits"] == [{"id": "<start_of_turn>", "snippet": "Human im_start"}]
    assert event["review_answers"] == {
        "said": "drop im_start",
        "relation": {"[INST]": "update"},
    }
    for key, given in [
        ("worktree_root", MINE),
        ("checkout", MINE),
        ("transcript_path", f"{MINE}/t.jsonl"),
        ("prior_session_id", f"{TOKEN}s1"),
    ]:
        assert event[key] == given, key


async def test_lookups_for_a_token_leave_the_scan_clean(
    server: tuple[Any, Store], capsys: pytest.CaptureFixture[str]
) -> None:
    """A search for a token, filtered by a client and a model spelled with
    one, and a missed search previewing a prompt that holds one: none is in
    the log, and the scan with --log reports nothing (the reviewer's probe
    went `found`, exit 1, for good)."""
    mcp, store = server
    _seed(store, "What im_start means in a template.")
    await call_tool(mcp, "memory_search", {"query": f"what does {TOKEN} mean"})
    await call_tool(
        mcp,
        "memory_search",
        {"query": "template", "client": TOKEN, "model": "[INST]"},
    )
    store.record_event(
        "search_miss",
        probe_query=f"explain <start_of_turn> in {TOKEN}",
        top_hits=[{"id": "x", "snippet": f"a body with {TOKEN} in it"}],
    )
    assert TOKEN not in _log_text(store)
    assert find_control_tokens(_log_text(store)) == []
    code, out, _ = _run(capsys, "--log", "--store", str(store.path))
    assert code == 0, out
    assert out.startswith(
        "bettermemory security scan: clean, 0 credential findings, "
        "0 control-token findings\n"
    )


# ---------------------------------------------------------------------------
# A checkout's path is a join key: kept as given, the joins hold
# ---------------------------------------------------------------------------


def test_the_stop_audit_anchor_finds_a_checkout_spelled_with_a_token(
    store: Store,
) -> None:
    """The round-2 review's probe: two windows, mine in the `[INST]`
    checkout and the other's event the latest. A renamed path sent the
    anchor to the other window's session."""
    store.record_event("search", session="sess_mine", worktree_root=MINE, query="x")
    store.record_event("search", session="sess_other", worktree_root=OTHER, query="y")
    events = list(store.iter_events())
    assert _latest_in_process_session(events, worktree_root=MINE) == "sess_mine"


def _at(kind: str, session: str, second: int, **fields: Any) -> dict[str, Any]:
    """A row as the event writer keeps it, stamped in `MINE`."""
    row = {"kind": kind, "session": session, "worktree_root": MINE, **fields}
    return {**_redact_event_fields(row), "ts": f"2026-10-11T10:00:{second:02d}Z"}


def test_the_foreign_window_filter_reads_a_checkout_spelled_with_a_token() -> None:
    """tests/test_harness_session.py's race in the `[INST]` checkout: a
    /clear in one window while another window's turn runs. Both servers
    wrote in the checkout since the clear, so the bridge sets both aside;
    with the path renamed in the rows it counted neither and took the
    other window's server."""
    before, after = "transcript-before-clear", "transcript-after-clear"
    events = [
        _at("turn_audited", before, 0, triggered_from="stop_hook"),
        _at("search", "sess_a", 1, harness_session=before),
        _at("turn_audited", "window-b", 2, triggered_from="stop_hook"),
        _at("search", "sess_b", 3, harness_session="window-b"),
        _at("turn_audited", after, 10, triggered_from="stop_hook"),
        _at("search", "sess_a", 11, harness_session=before),
        _at("search", "sess_b", 12, harness_session="window-b"),
    ]
    assert bridged_session(events, harness_session=after, worktree_root=MINE) is None


def test_the_handoff_finds_a_quiet_session_in_a_checkout_spelled_with_a_token(
    store: Store,
) -> None:
    """The handoff's quiet session (`_quiet_session`): a session whose
    brief named the checkout and that journaled nothing. The review's probe
    found the stored checkout no longer equal to the live one."""
    store.record_event(
        "session_brief", session_id="sess_quiet", checkout=MINE, ids=[], chars=0
    )
    (row,) = [e for e in store.iter_events() if e.get("kind") == "session_brief"]
    assert row["checkout"] == MINE
    quiet = _quiet_session(
        store, checkout=MINE, exclude=(), journaled=(), episodes_here=()
    )
    assert quiet is not None and quiet[0] == "sess_quiet"


async def test_a_handoff_refuses_a_session_id_carrying_a_token(
    server: tuple[Any, Store], capsys: pytest.CaptureFixture[str]
) -> None:
    """The round-3 review's token sweep: an explicit `prior_session_id` was
    the one argument that left a token in the store, kept as given in the
    handoff's row, where no command erases it and every scan with --log
    reported it. No session id holds one, so it is refused, and an id
    without one is looked up and kept as given."""
    mcp, store = server

    def handoffs() -> list[dict[str, Any]]:
        return [e for e in store.iter_events() if e.get("kind") == "episode_handoff"]

    with pytest.raises(ToolError) as exc:
        await call_tool(
            mcp, "episode", {"action": "handoff", "prior_session_id": f"{TOKEN}s1"}
        )
    text = str(exc.value)
    assert "`prior_session_id` carries a chat-template control token" in text, text
    assert "No session id holds one" in text and find_control_tokens(text) == []
    assert handoffs() == []
    reply = await call_tool(
        mcp, "episode", {"action": "handoff", "prior_session_id": "sess_given"}
    )
    assert reply["prior_session_id"] == "sess_given"
    (row,) = handoffs()
    assert row["prior_session_id"] == "sess_given"
    code, out, _ = _run(capsys, "--log", "--store", str(store.path))
    assert code == 0, out


# ---------------------------------------------------------------------------
# The caller's declared names: kept as given, named where a model reads them
# ---------------------------------------------------------------------------


def test_a_declared_client_or_model_name_is_kept_as_given() -> None:
    caller = identity.resolve(
        None,
        environ={identity.ENV_CLIENT: f"{TOKEN}claude", identity.ENV_MODEL: "[INST]x"},
    )
    assert (caller.actor.client, caller.actor.model) == (f"{TOKEN}claude", "[INST]x")


async def test_memory_show_names_the_actor_the_record_keeps_as_given(
    server: tuple[Any, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, store = server
    monkeypatch.setenv(identity.ENV_CLIENT, f"{TOKEN}claude")
    monkeypatch.setenv(identity.ENV_MODEL, "<start_of_turn>model")
    written = await call_tool(
        mcp, "memory_write", {"content": "The deploy runs nightly.", "scopes": ["x"]}
    )
    shown = await call_tool(mcp, "memory_show", {"id": written["id"]})
    actor = shown["actor"]
    assert (actor["client"], actor["model"]) == (
        "im_start claude",
        "start_of_turn model",
    )
    assert find_control_tokens(json.dumps(shown, ensure_ascii=False)) == []
    kept = store.get_memory(written["id"]).actor
    assert kept is not None and kept.client == f"{TOKEN}claude"


async def test_a_client_filter_spelled_as_declared_finds_the_record(
    server: tuple[Any, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round-2 review's sixth item: a name stored renamed never matched
    a filter spelled as declared. Both are kept as given now."""
    mcp, _store = server
    monkeypatch.setenv(identity.ENV_CLIENT, f"{TOKEN}claude")
    written = await call_tool(
        mcp, "memory_write", {"content": "The deploy runs nightly.", "scopes": ["x"]}
    )
    hits = await call_tool(
        mcp, "memory_search", {"query": "deploy", "client": f"{TOKEN}claude"}
    )
    assert [row["id"] for row in _rows(hits)] == [written["id"]]


# ---------------------------------------------------------------------------
# What a model reads: tool replies, the SDK's errors, the hooks' text
# ---------------------------------------------------------------------------


async def test_memory_show_names_a_checkout_path_and_an_older_body(
    server: tuple[Any, Store],
) -> None:
    """A record's origin is its writer's checkout, kept as given; a body
    written before 9.0.0rc14 can hold a token. The reply names both."""
    mcp, store = server
    origin = Origin(cwd=MINE, worktree_root=MINE)
    record = store.write(
        content=f"The {TOKEN} opener starts a turn.", scopes=["x"], origin=origin
    )
    shown = await call_tool(mcp, "memory_show", {"id": record.id})
    assert find_control_tokens(json.dumps(shown, ensure_ascii=False)) == []
    assert "llama-INST-tune" in json.dumps(shown)
    assert shown["body"].strip() == "The im_start opener starts a turn."
    kept = store.get_memory(record.id)
    assert kept.origin is not None and kept.origin.worktree_root == MINE


async def test_an_acknowledgement_names_the_id_it_could_not_find(
    server: tuple[Any, Store],
) -> None:
    """The round-2 review's success-path echo: acknowledge_miss's
    not_found and wrong_kind replies quoted the id as sent. The lookup
    reads the id as given, since an event's id is kept so."""
    mcp, store = server
    reason = "a fine reason for it"
    missing = await call_tool(
        mcp,
        "memory_admin",
        {"action": "acknowledge_miss", "id": TOKEN, "reason": reason},
    )
    assert (missing["status"], missing["event_id"]) == ("not_found", "im_start")
    store.record_event("use", event_id=f"{TOKEN}e1", ids=[], outcome="ignored")
    other = await call_tool(
        mcp,
        "memory_admin",
        {"action": "acknowledge_miss", "id": f"{TOKEN}e1", "reason": reason},
    )
    assert (other["status"], other["event_id"], other["kind"]) == (
        "wrong_kind",
        "im_start e1",
        "use",
    )
    assert find_control_tokens(json.dumps(other, ensure_ascii=False)) == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("memory_write", {"content": "A durable fact here.", "scopes": TOKEN}),
        ("memory_search", {"query": "x", "max_results": TOKEN}),
        ("memory_admin", {"action": TOKEN}),
        ("episode", {"action": TOKEN}),
    ],
)
async def test_the_sdks_argument_error_names_the_token(
    server: tuple[Any, Store], tool: str, args: dict[str, Any]
) -> None:
    """The round-2 review's probe: the SDK checks the arguments before the
    tool runs and quotes pydantic's `input_value` as sent."""
    mcp, _store = server
    with pytest.raises(ToolError) as exc:
        await call_tool(mcp, tool, args)
    text = str(exc.value)
    assert find_control_tokens(text) == [], text
    assert "im_start" in text and "validation error" in text, text


async def test_an_argument_error_over_the_wire_names_the_token(
    server: tuple[Any, Store],
) -> None:
    """The same errors as a client gets them: a `tools/call` through an
    in-process `mcp.Client`, so the request takes the SDK's own path to
    `call_tool` (the round-3 review's wire probe). An SDK release that
    routed a call past `_Served.call_tool` would leak the token here."""
    mcp, _store = server
    async with Client(mcp) as client:
        for tool, args in [
            ("memory_write", {"content": "A durable fact here.", "scopes": TOKEN}),
            ("memory_search", {"query": "x", "max_results": TOKEN}),
            ("memory_admin", {"action": TOKEN}),
            (TOKEN, {}),
        ]:
            result = await client.call_tool(tool, args)
            text = " ".join(getattr(block, "text", "") for block in result.content)
            assert result.is_error, (tool, text)
            assert find_control_tokens(text) == [] and "im_start" in text, text


def test_hook_text_is_kept_with_each_token_named() -> None:
    text = f"checkout {MINE} asks: which project? {TOKEN}"
    result = HookResult(stdout=text, surface="session_brief", chars=len(text))
    assert (
        result.stdout
        == "checkout /Users/u/src/llama-INST-tune asks: which project? im_start"
    )
    assert result.chars == len(result.stdout)
    assert result.to_response()["stdout"] == result.stdout
    plain = "no token here"
    assert HookResult(stdout=plain, surface="prompt_recall").stdout is plain


def test_the_prompt_recall_names_a_token_in_an_older_body(tmp_path: Path) -> None:
    """A body written before 9.0.0rc14 holds a token: the pointer's summary
    in the hook's text and the hit's snippet in the row name it."""
    world = PromptWorld(tmp_path)
    memory = world.write(f"backup strategy uses {TOKEN} triangular restic replication")
    shown = world.recall()
    assert memory.id in shown.stdout
    assert find_control_tokens(shown.stdout) == [] and "im_start" in shown.stdout
    assert shown.chars == len(shown.stdout)
    (row,) = world.recalls()
    assert find_control_tokens(json.dumps(row["top_hits"], ensure_ascii=False)) == []


# ---------------------------------------------------------------------------
# A refusal's text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("memory_show", {"id": TOKEN}),
        ("memory_remove", {"id": TOKEN, "reason": "stale"}),
        ("memory_search", {"query": "x", "scopes": [TOKEN]}),
        ("memory_search", {"query": "x", "mode": "[INST]"}),
        (
            "memory_write",
            {"content": "A clean body.", "scopes": ["x"], "supersedes": [TOKEN]},
        ),
        ("memory_admin", {"action": "unjoin_project", "id": TOKEN}),
        (
            "episode",
            {"action": "write", "body": "b", "takeaway": "t", "swarm_id": TOKEN},
        ),
    ],
)
async def test_a_refusal_quoting_the_caller_names_the_token(
    server: tuple[Any, Store], tool: str, args: dict[str, Any]
) -> None:
    """An invalid id, scope, mode or swarm id is quoted back by its refusal
    (pydantic's `input_value` among them): with each token by its name."""
    mcp, _store = server
    with pytest.raises(ToolError) as exc:
        await call_tool(mcp, tool, args)
    text = str(exc.value)
    assert find_control_tokens(text) == [], text
    assert "im_start" in text or "INST" in text, text


# ---------------------------------------------------------------------------
# Rows written before 9.0.0rc18
# ---------------------------------------------------------------------------


def test_the_health_report_names_a_token_an_older_row_holds() -> None:
    """A missed search's preview (the user's prompt) and a resolution
    note, as a row from before the event writer named tokens holds them."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_event_id(
            m.id, "EVID_A", ts=_utc(2026, 4, 5), query_preview=f"why {TOKEN} here"
        ),
        _event(
            "use",
            ts=_utc(2026, 4, 3),
            ids=[m.id],
            outcome="contradicted",
            note=f"{TOKEN}system wrong",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert report.recent_silent_misses[0].query_preview == "why im_start here"
    (row,) = report.contradicted
    assert row.resolution_timeline[-1]["note"] == "im_start system wrong"


async def test_a_search_names_a_token_an_older_use_row_holds(
    server: tuple[Any, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, store = server
    written = await call_tool(
        mcp,
        "memory_write",
        {"content": "python list comprehension notes", "scopes": ["tools"]},
    )
    _as_before(
        store,
        monkeypatch,
        "use",
        ids=[written["id"]],
        outcome="ignored",
        note=f"{TOKEN}system not now",
        claim_excerpts=["[INST] the excerpt"],
    )
    assert TOKEN in _log_text(store)
    hits = await call_tool(mcp, "memory_search", {"query": "python"})
    (entry,) = _rows(hits)[0]["recent_negative_outcomes"]
    assert (entry["note"], entry["claim_excerpt"]) == (
        "im_start system not now",
        "INST the excerpt",
    )
