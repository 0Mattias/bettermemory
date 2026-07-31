"""`accept_proposal` runs the SHARED write-gate chain, not a private copy.

The review surface used to run one gate of six — a hand-rolled credential
scan — while the commit that extracted the shared chain described the
non-MCP writers as keeping "deliberately stricter copies". Stricter was the
opposite of true here: a body that `memory_write` refused as transient, as
scope-mismatched, or as a near-duplicate of something already stored landed
in the store unremarked as soon as it arrived through the proposal queue
instead. Every test below fails against that shape.

Two of the properties are structural rather than behavioural, and both
guard a trap the conversion could fall into later:

* the accept path passes `CONTENT_GATES` ITSELF, so a gate added to the
  chain reaches this surface instead of being silently skipped — and the
  two gates `CONTENT_GATES` excludes stay excluded, because the extractor
  stamps explicit captures ("remember that I prefer X") as `fact` and
  `UserClaimGate` would hard-refuse exactly the entries this queue exists
  to carry;
* `consolidate` is NOT converted. Its stamped-vs-unstamped scan split is a
  measured decision one `GateContext` cannot express, and "make the last
  writer consistent too" is the obvious next commit.

The dedup refusals are a DELIBERATE behaviour change: accepting a proposal
that near-duplicates an existing memory used to succeed (dedup was
documented as the reviewer's job) and is now a hard `duplicate` refusal
with `force` as the escape.

The last section is about the OTHER end of the same override: a refusal is
only answerable if the flag that answers it reaches the handler, and the new
overrides shipped dead at the MCP boundary once already (`_handlers.py`'s
facade signature IS the served schema, and the facade was not mirrored). The
two whole-surface tests there generalize that check to every registered tool
rather than re-adding it one incident at a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.handlers import proposals as accept_module
from bettermemory.handlers.proposals import (
    DESC_MEMORY_PROPOSALS,
    _CLI_ESCAPE_FLAGS,
    accept_proposal,
)
from bettermemory.handlers.write import (
    CONTENT_GATES,
    CredentialGate,
    DedupActiveGate,
    DedupTombstoneGate,
    GroundednessGate,
    PendingGate,
    ScopeMismatchGate,
    TransientGate,
    UserClaimGate,
    apply_write_gates,
)
from bettermemory.proposals import Proposal, ProposalQueue
from bettermemory.store import Store
from ._mcp import input_schema as _input_schema

_CREATED = "2026-01-01T12:00:00+00:00"


def _queue(
    root: Path, body: str, *, pid: str = "p1", cat: str = "fact"
) -> ProposalQueue:
    queue = ProposalQueue(root)
    queue.append(
        [
            Proposal(
                id=pid,
                body=body,
                source_excerpt=body,
                suggested_category=cat,
                created=_CREATED,
            )
        ]
    )
    return queue


def _accept(root: Path, **kwargs: Any) -> dict[str, Any]:
    """Call the accept core the way both entry points do."""
    kwargs.setdefault("proposal_id", "p1")
    kwargs.setdefault("scopes", ["tools"])
    return accept_proposal(
        store=Store(root),
        config=Config(storage=StorageConfig(directory=str(root))),
        recorder=Recorder(root=root, session_id="sess_gate_parity"),
        **kwargs,
    )


def _accept_events(root: Path) -> list[dict[str, Any]]:
    return [
        e
        for e in iter_events(root)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]


# ---------------------------------------------------------------------------
# Structural: which gates the accept path actually runs
# ---------------------------------------------------------------------------


def test_accept_runs_the_shared_chain_object_not_a_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tuple handed to `apply_write_gates` is `CONTENT_GATES` itself.

    Identity, not equality: a future edit that narrows the tuple at this
    call site (the shape the hand-rolled scan effectively had — one gate,
    hard-coded) is the regression, and a subset compares equal to nothing
    that would notice. Pinning identity means a gate ADDED to the shared
    chain reaches this surface by construction, which is the whole point of
    the chokepoint."""
    _queue(tmp_path, "The deploy pipeline builds an image before it promotes it.")
    seen: dict[str, Any] = {}
    # The name bound on the accept module IS the shared entry point, not a
    # same-named private copy — which every other assertion here would miss,
    # because the spy below replaces that binding and then delegates to the
    # real function regardless of what it displaced. Read out of `vars()`
    # rather than as an attribute: `apply_write_gates` is imported into
    # `handlers.proposals`, not exported by it, and `no_implicit_reexport`
    # rejects reaching through a module for a name it does not own.
    assert vars(accept_module)["apply_write_gates"] is apply_write_gates
    real = apply_write_gates

    def spy(deps: Any, gc: Any, *, gates: Any) -> Any:
        seen["gates"] = gates
        seen["gc"] = gc
        return real(deps, gc, gates=gates)

    monkeypatch.setattr(accept_module, "apply_write_gates", spy)
    result = _accept(tmp_path)

    assert result["status"] == "accepted"
    assert seen["gates"] is CONTENT_GATES
    # The gate reads the body that would be PERSISTED, not the excerpt.
    assert seen["gc"].payload["content"].startswith("The deploy pipeline")


def test_the_accept_chain_is_exactly_the_six_content_gates() -> None:
    """Membership is a decision, not an accident.

    `CONTENT_GATES` is derived by EXCLUSION, so a new `WriteGate` joins it
    automatically and starts refusing proposals with every acknowledge flag
    at its default. Enumerating the members here means adding a gate forces
    someone to look at this surface and decide — the failure message is the
    prompt."""
    assert tuple(type(g) for g in CONTENT_GATES) == (
        CredentialGate,
        TransientGate,
        ScopeMismatchGate,
        GroundednessGate,
        DedupActiveGate,
        DedupTombstoneGate,
    ), (
        "the content-gate chain changed. `accept_proposal` runs it whole, so "
        "a new gate now judges proposal acceptance with every acknowledge "
        "flag False — decide whether the review surface should inherit it "
        "(and whether it needs an override on the MCP tool AND the CLI) "
        "before updating this list."
    )


def test_the_two_human_in_the_loop_gates_stay_out() -> None:
    """`UserClaimGate` refuses in order to route a write INTO the pending
    handshake, and `PendingGate` IS that handshake. Accepting a proposal is
    already the human's review decision, and the extractor deliberately
    stamps explicit captures ("remember that I prefer X") as `fact` — so
    their bodies match the preference shapes by construction. Inheriting
    either gate would hard-refuse exactly what the queue exists to carry."""
    assert not any(isinstance(g, (UserClaimGate, PendingGate)) for g in CONTENT_GATES)


def test_a_user_claim_shaped_proposal_still_accepts(tmp_path: Path) -> None:
    """The behavioural half of the exclusion above, at this surface."""
    _queue(tmp_path, "I prefer terse code-driven explanations over long prose.")
    result = _accept(tmp_path, scopes=["learning-style"])
    assert result["status"] == "accepted"
    assert len(Store(tmp_path).load_all()) == 1


# ---------------------------------------------------------------------------
# The gates that were unreachable before, one behaviour each
# ---------------------------------------------------------------------------


def test_a_transient_proposal_is_refused_and_stays_queued(tmp_path: Path) -> None:
    """AC: a transient-marker proposal cannot be accepted unwarned.

    Before the conversion this body was written durably through the review
    surface while `memory_write` refused the identical text — the queue was
    an end-run around the durability gate."""
    queue = _queue(tmp_path, "We are currently running the API on a single box.")
    result = _accept(tmp_path)

    assert result["status"] == "transient_warning"
    # The surface shape survives the swap: every memory_proposals result
    # carries the action and the id of the entry it is about.
    assert result["action"] == "accept"
    assert result["proposal_id"] == "p1"
    assert "currently" in {m["marker"] for m in result["markers"]}
    # Refused BEFORE the claim: still queued, nothing written, no event.
    assert [p.id for p in queue.load()] == ["p1"]
    assert Store(tmp_path).load_all() == []
    assert _accept_events(tmp_path) == []


def test_acknowledge_transient_accepts_and_records_the_override(
    tmp_path: Path,
) -> None:
    """The escape hatch, and its override-rate evidence. A marker list that
    is never revisited is a marker list nobody can tune, so the acknowledged
    markers land in the result AND in the audit log."""
    _queue(tmp_path, "We are currently running the API on a single box.")
    result = _accept(tmp_path, acknowledge_transient=True)

    assert result["status"] == "accepted"
    assert result["markers_acknowledged"] == ["currently"]
    (event,) = _accept_events(tmp_path)
    assert event["markers_acknowledged"] == ["currently"]
    assert len(Store(tmp_path).load_all()) == 1


def test_a_duplicate_proposal_is_refused_and_stays_queued(tmp_path: Path) -> None:
    """The deliberate behaviour change: dedup stops being the reviewer's job.

    Accepting a near-duplicate used to succeed silently, leaving two
    parallel entries making the same claim — the exact outcome
    `memory_write` refuses and routes to `memory_update` instead."""
    store = Store(tmp_path)
    body = "Releases are cut from main only after the full CI matrix is green."
    existing = store.write(content=body, scopes=["tools"])
    queue = _queue(tmp_path, body)

    result = _accept(tmp_path)

    assert result["status"] == "duplicate"
    assert result["action"] == "accept"
    assert result["proposal_id"] == "p1"
    assert existing.id in {m["id"] for m in result["matches"]}
    assert "memory_update" in result["hint"]
    assert [p.id for p in queue.load()] == ["p1"]
    assert [m.id for m in store.load_all()] == [existing.id]
    assert _accept_events(tmp_path) == []


def test_force_overrides_the_duplicate_refusal(tmp_path: Path) -> None:
    """The escape the refusal's own hint offers, wired to this surface."""
    store = Store(tmp_path)
    body = "Releases are cut from main only after the full CI matrix is green."
    store.write(content=body, scopes=["tools"])
    _queue(tmp_path, body)

    result = _accept(tmp_path, force=True)

    assert result["status"] == "accepted"
    assert len(store.load_all()) == 2
    (event,) = _accept_events(tmp_path)
    assert event["forced"] is True


def test_a_previously_removed_proposal_is_refused_then_force_accepts(
    tmp_path: Path,
) -> None:
    """`force` bypasses BOTH dedup gates here, exactly as on `memory_write`.

    Deliberately unlike `ingest --force`, whose contract keeps tombstone
    dedup ON: there, force is an unattended re-import flag; here it is a
    reviewer answering the `previously_removed` hint, which offers force by
    name. If force skipped only the active-set gate, that hint would name an
    override that does not work."""
    store = Store(tmp_path)
    body = "The nightly rebuild job was replaced by an on-demand trigger."
    removed = store.write(content=body, scopes=["tools"])
    store.tombstone(removed.id, reason="superseded", session_id="sess_gate_parity")
    queue = _queue(tmp_path, body)

    refused = _accept(tmp_path)
    assert refused["status"] == "previously_removed"
    assert removed.id in {m["id"] for m in refused["removed_matches"]}
    assert "memory_restore" in refused["hint"]
    assert [p.id for p in queue.load()] == ["p1"]
    assert store.load_all() == []

    forced = _accept(tmp_path, force=True)
    assert forced["status"] == "accepted"
    assert len(store.load_all()) == 1


def test_a_scope_mismatched_proposal_is_refused_then_acknowledged(
    tmp_path: Path,
) -> None:
    """The gate needs a NON-EMPTY store to fire at all: it derives the known
    project names from existing memories, so an empty-store fixture disables
    it structurally and proves nothing."""
    store = Store(tmp_path)
    store.write(
        content="The webapp ships from a container image.", scopes=["projects:webapp"]
    )
    queue = _queue(tmp_path, "The webapp deploy runs through GitHub Actions.")

    refused = _accept(tmp_path, scopes=["tools"])
    assert refused["status"] == "scope_mismatch"
    assert "projects:webapp" in refused["suggested_scopes"]
    assert [p.id for p in queue.load()] == ["p1"]
    assert len(store.load_all()) == 1

    accepted = _accept(tmp_path, scopes=["tools"], acknowledge_scope_mismatch=True)
    assert accepted["status"] == "accepted"
    assert len(store.load_all()) == 2


# ---------------------------------------------------------------------------
# The invariant the conversion must not break
# ---------------------------------------------------------------------------


def test_a_refused_accept_leaves_the_store_directory_byte_identical(
    tmp_path: Path,
) -> None:
    """ "Nothing here ever writes to the memory store" until an accept lands.

    The gates now READ the store on every accept (`load_all`,
    `load_tombstones`) — three reads where there used to be none — so the
    invariant is worth asserting whole rather than per-file. Snapshot every
    path and its bytes, refuse an accept, compare."""
    store = Store(tmp_path)
    store.write(
        content="Releases are cut from main after CI is green.", scopes=["tools"]
    )
    _queue(tmp_path, "Releases are cut from main after CI is green.")

    def snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(tmp_path)): p.read_bytes()
            for p in sorted(tmp_path.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    assert _accept(tmp_path)["status"] == "duplicate"
    assert snapshot() == before


# ---------------------------------------------------------------------------
# The refusals have to be answerable from both entry points
# ---------------------------------------------------------------------------


def test_every_reachable_refusal_names_its_cli_override(tmp_path: Path) -> None:
    """The credential hatch shipped DEAD at the CLI once: the core took the
    parameter, the command had no flag, and the refusal told the operator to
    pass something the CLI could not express. Every gate refusal reachable
    from here now names both spellings, so that failure cannot recur one
    gate at a time."""
    store = Store(tmp_path)
    body = "Releases are cut from main only after the full CI matrix is green."
    store.write(content=body, scopes=["tools"])
    _queue(tmp_path, body)
    refused = _accept(tmp_path)
    assert refused["status"] == "duplicate"
    assert _CLI_ESCAPE_FLAGS["duplicate"] in refused["hint"]
    # The MCP spelling survives the append — the hint is the gate's own.
    assert "force=True" in refused["hint"]


def test_the_desc_status_vocabulary_covers_every_refusal() -> None:
    """`DESC_MEMORY_PROPOSALS` enumerates what `status` can be. A refusal the
    model has never been told about reads as an unexplained failure, and the
    conversion added four of them at once."""
    for status in _CLI_ESCAPE_FLAGS:
        assert f"`{status}`" in DESC_MEMORY_PROPOSALS, (
            f"{status} is reachable from memory_proposals(action='accept') "
            "but absent from the tool description's status vocabulary"
        )
    for override in (
        "acknowledge_credential",
        "acknowledge_transient",
        "acknowledge_scope_mismatch",
        "force=True",
    ):
        assert override in DESC_MEMORY_PROPOSALS


def test_cli_accept_surfaces_the_duplicate_refusal_and_the_force_flag(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Through the REAL argparse boundary, both halves: the refusal is a
    clean exit 2 that names the matched id and the flag, and the flag then
    actually reaches the core. A flag wired into the parser but dropped on
    the way to `accept_proposal` passes every handler-level test in this
    file."""
    import sys as _sys

    from bettermemory.config import load_config
    from bettermemory.server import main as cli_main

    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    store = Store(load_config().resolved_directory())
    body = "Releases are cut from main only after the full CI matrix is green."
    existing = store.write(content=body, scopes=["tools"])
    _queue(store.root, body)

    def run_cli(*argv: str) -> None:
        monkeypatch.setattr(_sys, "argv", ["bettermemory", *argv])
        cli_main()

    with pytest.raises(SystemExit) as exc:
        run_cli("proposals", "accept", "p1", "--scope", "tools")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "duplicates an existing memory" in err
    assert existing.id in err
    assert "--force" in err
    assert [p.id for p in ProposalQueue(store.root).load()] == ["p1"]
    assert len(store.load_all()) == 1

    run_cli("proposals", "accept", "p1", "--scope", "tools", "--force")
    assert "Accepted" in capsys.readouterr().out
    assert ProposalQueue(store.root).load() == []
    assert len(store.load_all()) == 2


def test_cli_accept_json_refusal_stays_data_on_exit_zero(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The --json lane treats every refusal as data, not just the credential
    one it was written for. Generalizing the human branch by status is what
    keeps the two lanes in step; enumerating statuses there would let a new
    refusal fall through to the "Accepted" line and report a write that
    never happened."""
    import sys as _sys

    from bettermemory.config import load_config
    from bettermemory.server import main as cli_main

    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    store = Store(load_config().resolved_directory())
    _queue(store.root, "We are currently running the API on a single box.")

    monkeypatch.setattr(
        _sys,
        "argv",
        ["bettermemory", "proposals", "accept", "p1", "--scope", "tools", "--json"],
    )
    cli_main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "transient_warning"
    assert payload["proposal_id"] == "p1"
    assert [p.id for p in ProposalQueue(store.root).load()] == ["p1"]


async def _served_schemas(root: Path) -> dict[str, set[str]]:
    """Tool name -> the keys a real client may actually send.

    Built from a REALLY built server's `list_tools()` rather than from any
    signature, because the signature is the input to the thing under test.
    `full_tool_surface` is on so the gated curation tools — `memory_proposals`
    among them — are registered and comparable too."""
    from bettermemory.builder import build_server
    from bettermemory.config import BehaviorConfig
    from bettermemory.session import SessionState

    server = build_server(
        config=Config(
            storage=StorageConfig(directory=str(root)),
            behavior=BehaviorConfig(full_tool_surface=True),
        ),
        store=Store(root),
        state=SessionState(),
    )
    return {
        t.name: set(_input_schema(t).get("properties", {}))
        for t in await server.list_tools()
    }


async def test_the_new_overrides_reach_the_registered_mcp_schema(
    tmp_path: Path,
) -> None:
    """The SDK derives the input schema from the `ToolHandlers` facade, whose
    pydantic arg-model silently DROPS any key the facade signature does not
    declare. A handler parameter the facade omits is dead at the tool
    boundary — the client passes `force=True` and still gets the refusal.
    That is exactly how `acknowledge_credential` shipped dead once, so this
    asserts PARITY over the whole signature rather than one flag at a time."""
    import inspect

    served = (await _served_schemas(tmp_path))["memory_proposals"]
    declared = {
        name
        for name in inspect.signature(accept_module.memory_proposals).parameters
        if name not in {"deps", "ctx"}
    }
    assert declared <= served, (
        f"memory_proposals handler parameters {sorted(declared - served)} are "
        "not on the registered input schema — mirror them in the "
        "`ToolHandlers.memory_proposals` facade in `src/bettermemory/"
        "_handlers.py` (signature AND the forwarded call), or they are dead "
        "at the MCP boundary."
    )


# ---------------------------------------------------------------------------
# The same trap, generalized: the facade IS the wire schema for EVERY tool
#
# The two tests below are not about proposals. They live here because the
# proposals overrides are the second parameter set in one phase to ship dead
# at the MCP boundary (`acknowledge_credential` was the first), and a guard
# that covers one tool at a time is a guard that gets added one incident at
# a time. Fold them into `tests/test_tool_surface.py` if that file ever
# grows a schema section.
# ---------------------------------------------------------------------------


async def test_no_handler_parameter_is_dead_at_the_mcp_boundary(
    tmp_path: Path,
) -> None:
    """Whole-surface signature parity: every registered tool, both directions.

    `declared - served` is the shipped-dead defect: an override the handler
    honours that no client can send. `served - declared` is its mirror — a
    key the wire accepts and the delegation then rejects as an unexpected
    keyword, i.e. a TypeError only a live client sees."""
    import inspect

    from bettermemory import handlers as handlers_pkg

    served_by_tool = await _served_schemas(tmp_path)
    drift: dict[str, dict[str, list[str]]] = {}
    for name, served in served_by_tool.items():
        handler = getattr(handlers_pkg, name, None)
        assert handler is not None, (
            f"tool {name} is registered but `bettermemory.handlers.{name}` "
            "does not exist — this test would silently skip it"
        )
        declared = {
            p for p in inspect.signature(handler).parameters if p not in {"deps", "ctx"}
        }
        if declared != served:
            drift[name] = {
                "handler_only_dead_on_the_wire": sorted(declared - served),
                "wire_only_would_TypeError": sorted(served - declared),
            }

    assert not drift, (
        f"`ToolHandlers` in `src/bettermemory/_handlers.py` has drifted from "
        f"the handler signatures it fronts: {drift}. The facade signature IS "
        "the served schema — mirror the parameter there (signature AND the "
        "forwarded call) or drop it from the handler."
    )


def test_every_facade_parameter_is_actually_forwarded() -> None:
    """Declaring the parameter is only half of it.

    A facade method that takes `force` and then calls the handler without it
    passes every schema-shaped check above — the key rides the wire, binds to
    the facade, and is dropped one frame before the gate that reads it. The
    delegation is uniform enough (`return await _handlers_pkg.<tool>(self,
    …)`) to check structurally."""
    import ast
    import inspect

    from bettermemory._handlers import ToolHandlers

    source = (
        Path(__file__).resolve().parents[1] / "src" / "bettermemory" / "_handlers.py"
    ).read_text(encoding="utf-8")
    facade = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "ToolHandlers"
    )

    dropped: dict[str, list[str]] = {}
    checked: set[str] = set()
    for method in facade.body:
        if not isinstance(method, ast.AsyncFunctionDef) or method.name.startswith("_"):
            continue
        delegation = next(
            (
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method.name
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_handlers_pkg"
            ),
            None,
        )
        assert delegation is not None, (
            f"`ToolHandlers.{method.name}` no longer delegates to "
            f"`_handlers_pkg.{method.name}`; this check cannot see what it "
            "forwards. Re-read the delegation convention in the class "
            "docstring before landing that."
        )
        checked.add(method.name)
        forwarded = {a.id for a in delegation.args if isinstance(a, ast.Name)}
        forwarded |= {
            kw.arg
            for kw in delegation.keywords
            if kw.arg and isinstance(kw.value, ast.Name) and kw.value.id == kw.arg
        }
        gap = [
            a.arg
            for a in (*method.args.args, *method.args.kwonlyargs)
            if a.arg != "self" and a.arg not in forwarded
        ]
        if gap:
            dropped[method.name] = gap

    # The AST walk has to have SEEN every tool method — a rename or a move
    # that makes the loop body run zero times would otherwise pass silently.
    assert checked == {
        name
        for name, value in vars(ToolHandlers).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(value)
    }
    assert not dropped, (
        f"these facade parameters reach the wire and are then dropped on the "
        f"way to the handler: {dropped}. A silently ignored override is worse "
        "than a missing one — the client is told it was honoured."
    )


# ---------------------------------------------------------------------------
# What the conversion must NOT touch
# ---------------------------------------------------------------------------


def test_consolidate_keeps_its_hand_rolled_gates() -> None:
    """`consolidate`'s `propose_new` branch is the one write path that stays
    hand-rolled, and "make the last writer consistent too" is the obvious
    next commit.

    Its split is MEASURED, not an oversight: the credential scan runs on the
    provenance-STAMPED body (a secret can ride in the verbatim excerpt),
    while the transient and dedup scans run on the UNSTAMPED proposal body
    (stamped-vs-stamped Jaccard measured 0.882 against 0.10 unstamped, so
    scanning stamped text bounces sibling facts from one turn as
    duplicates). One `GateContext.payload` cannot express two different
    bodies, so converting it inverts both decisions at once."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "bettermemory" / "consolidate.py"
    ).read_text(encoding="utf-8")
    assert "apply_write_gates" not in source, (
        "consolidate now calls the shared gate chain. Its stamped-vs-"
        "unstamped scan split is a measured decision one GateContext cannot "
        "express — re-read the comments around its credential and dedup "
        "scans before landing this."
    )
    assert "body_with_provenance" in source
