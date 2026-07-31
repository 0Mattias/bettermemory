"""The served schemas carry no pydantic `title` annotations, and that is free.

Pydantic generates a `title` for every property (`content` ->
`"title": "Content"`) and one for every schema (`"title":
"memory_writeArguments"`). None of it is load-bearing: `title` is a
display annotation in JSON Schema, nothing validates against it, and no
in-tree consumer reads it. It cost 2,812 chars of the lean surface —
2,119 on `inputSchema`, 693 on `outputSchema` — paid by every client on
every turn, including the turns that never touch memory.

`builder._strip_schema_titles` deletes them after registration. The SDK
offers no hook (`Tool.from_function` hard-codes
`parameters = arg_model.model_json_schema(by_alias=True)`), so the scrub
reaches through `FastMCP._tool_manager._tools` — a private attribute,
feature-detected rather than version-pinned, because the `mcp>=1.0.0`
floor is an install-compat promise and raising it to protect a size
optimisation would trade a real break for a saving.

WHAT THIS MODULE HAS TO PROVE, and why each proof is shaped the way it is:

* The saving is real and the diff is *only* title deletions
  (`test_served_schemas_are_the_pydantic_schemas_minus_titles`). Checked
  against an independent, functional re-implementation of the stripper
  rather than against a recorded table, so a bug shared with the
  in-place version cannot hide.
* Call behaviour cannot change, and the reason is structural: the
  emitted dict is not on the call path at all
  (`test_the_served_schema_is_not_on_the_call_path`). That test corrupts
  a served schema and shows the call is unmoved — which is a stronger
  claim than "titles do not matter", and it is the claim that makes the
  scrub safe.
* THE FAILURE MODE IS A SILENT NO-OP. If a future SDK moves the registry,
  `getattr` returns None, the scrub returns quietly, and every schema
  quietly regrows — with no diff in this repo.
  `test_the_scrub_is_not_a_silent_no_op` fails in that world, and
  distinguishes it from the benign one where the SDK simply stopped
  emitting titles. `tests/test_resident_footprint.py`'s remainder ceiling
  is the second net: at 7,500 it does not fit the un-scrubbed 9,881.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate

from bettermemory.builder import _strip_titles, build_server
from bettermemory.config import (
    BehaviorConfig,
    Config,
    ProposalsConfig,
    StorageConfig,
)
from bettermemory.session import SessionState
from bettermemory.store import Store
from ._mcp import (
    input_schema as _input_schema,
    probe_server,
    output_schema as _output_schema,
)


def _server(tmp_path: Path, *, full_surface: bool = True) -> Any:
    """The FULL surface, so the scrub is checked on all 27 tools — the lean
    surface is what `tests/test_resident_footprint.py` budgets, but a
    title that survives on a gated tool is still a title on the wire for
    anyone running `full_tool_surface`."""
    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(full_tool_surface=full_surface),
        proposals=ProposalsConfig(),
    )
    return build_server(config=cfg, store=Store(tmp_path), state=SessionState())


def _blob(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _without_titles(node: Any) -> Any:
    """Independent re-implementation of the scrub — FUNCTIONAL (returns new
    objects) where the shipped one mutates in place.

    Deliberately not imported from `builder`: this is the oracle the served
    schemas are checked against, and an oracle that is the same code as the
    thing under test proves only that the code equals itself."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "title":
                continue
            if key in ("properties", "$defs", "definitions") and isinstance(
                value, dict
            ):
                out[key] = {k: _without_titles(v) for k, v in value.items()}
            else:
                out[key] = _without_titles(value)
        return out
    if isinstance(node, list):
        return [_without_titles(item) for item in node]
    return node


def _title_count(node: Any) -> int:
    """Schema-node titles only — a property KEYED `title` is not one."""
    total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "title":
                total += 1
            elif key in ("properties", "$defs", "definitions") and isinstance(
                value, dict
            ):
                for sub in value.values():
                    total += _title_count(sub)
            else:
                total += _title_count(value)
    elif isinstance(node, list):
        for item in node:
            total += _title_count(item)
    return total


# ---------------------------------------------------------------------------
# The saving, and that it is the ONLY change
# ---------------------------------------------------------------------------


async def test_no_title_survives_on_the_served_surface(tmp_path: Path) -> None:
    """The wire-visible outcome, stated as the thing a client would see."""
    tools = await _server(tmp_path).list_tools()
    offenders = {
        f"{t.name}.{leg}": _title_count(schema)
        for t in tools
        for leg, schema in (("input", _input_schema(t)), ("output", _output_schema(t)))
        if schema is not None and _title_count(schema)
    }
    assert not offenders, (
        f"pydantic `title` annotations reached the wire on {offenders}. Every "
        f"client pays them on every turn; `builder._strip_schema_titles` is "
        f"supposed to have deleted them after registration."
    )


async def test_served_schemas_are_the_pydantic_schemas_minus_titles(
    tmp_path: Path,
) -> None:
    """The whole diff, tool by tool and leg by leg.

    Reconstructs what pydantic emits for each tool from a throwaway
    `FastMCP` that never went through the scrub, and requires the served
    schema to be exactly that minus its titles. Anything else the scrub
    touched — a reordered key, a dropped constraint, a mangled `anyOf` —
    fails here rather than in a client six months later."""
    mcp = _server(tmp_path)
    served = {t.name: t for t in await mcp.list_tools()}
    registry = mcp._tool_manager._tools

    for name, tool in registry.items():
        pristine = probe_server("title-scrub-oracle")
        pristine.tool(name=name, description=tool.description)(tool.fn)
        (reference,) = await pristine.list_tools()

        assert _blob(_without_titles(_input_schema(reference))) == _blob(
            _input_schema(served[name])
        ), (
            f"{name}: the served inputSchema is not the pydantic schema minus "
            f"its titles. The scrub changed something else."
        )
        assert (_output_schema(reference) is None) == (
            _output_schema(served[name]) is None
        ), f"{name}: the scrub changed whether an outputSchema is served at all"
        if _output_schema(reference) is not None:
            assert _blob(_without_titles(_output_schema(reference))) == _blob(
                _output_schema(served[name])
            ), f"{name}: the served outputSchema is not the reference minus titles"


async def test_properties_and_required_survive_the_scrub(tmp_path: Path) -> None:
    """The membership other tests pin by name — `acknowledge_credential` in
    three tools' properties, `include_bodies` / `ids` on `episode_search`
    — has to be membership the scrub cannot touch. Checked structurally
    for every tool rather than for the handful that happen to be named
    elsewhere."""
    mcp = _server(tmp_path)
    served = {t.name: t for t in await mcp.list_tools()}

    for name, tool in mcp._tool_manager._tools.items():
        pristine = probe_server("title-scrub-oracle")
        pristine.tool(name=name, description=tool.description)(tool.fn)
        (reference,) = await pristine.list_tools()
        schema = _input_schema(served[name])
        assert set(schema.get("properties", {})) == set(
            _input_schema(reference).get("properties", {})
        ), f"{name}: `properties` membership moved"
        assert schema.get("required") == _input_schema(reference).get("required"), (
            f"{name}: `required` moved"
        )
        assert schema.get("type") == _input_schema(reference).get("type")
        # And each property's body is intact apart from its own title.
        for param, body in _input_schema(reference).get("properties", {}).items():
            assert _blob(_without_titles(body)) == _blob(schema["properties"][param]), (
                f"{name}.{param}: the property body changed, not just its title"
            )


# ---------------------------------------------------------------------------
# The guard against a silent no-op
# ---------------------------------------------------------------------------


async def test_the_scrub_is_not_a_silent_no_op(tmp_path: Path) -> None:
    """A scrub that stops finding anything is the failure this file exists for.

    It has no symptom in this repo: the `getattr` chain returns None, the
    function logs at debug and returns, and every schema silently regrows
    by the amount below. So the guard is two-sided, and the sides mean
    different things:

    * Pydantic still emits titles (the SDK has not made this redundant).
      If THIS is what fails, the scrub is now dead code and should be
      retired deliberately, not left in place.
    * The served surface has none of them, and the gap between the two is
      the saving. If THIS is what fails, the scrub has stopped reaching
      the registry — check `_strip_schema_titles`'s feature detection
      against the installed SDK.
    """
    probe = probe_server("title-scrub-noop-probe")

    def _shaped_like_a_tool(content: str, force: bool = False) -> dict[str, Any]:
        return {}

    probe.tool(name="probe", description="d")(_shaped_like_a_tool)
    (unscrubbed,) = await probe.list_tools()
    assert _title_count(_input_schema(unscrubbed)) > 0, (
        "pydantic no longer emits `title` annotations, so there is nothing "
        "for `builder._strip_schema_titles` to strip. It is now dead code: "
        "retire it and the ceiling that assumes it, rather than leaving a "
        "no-op wired into every server build."
    )

    tools = await _server(tmp_path).list_tools()
    scrubbed = sum(
        len(_blob(_input_schema(t)))
        + (len(_blob(_output_schema(t))) if _output_schema(t) is not None else 0)
        for t in tools
    )
    unstripped = 0
    for tool in _server(tmp_path)._tool_manager._tools.values():
        pristine = probe_server("title-scrub-oracle")
        pristine.tool(name=tool.name, description=tool.description)(tool.fn)
        (reference,) = await pristine.list_tools()
        unstripped += len(_blob(_input_schema(reference)))
        if _output_schema(reference) is not None:
            unstripped += len(_blob(_output_schema(reference)))

    saving = unstripped - scrubbed
    assert saving > 2_000, (
        f"the title scrub saved {saving} chars across the served surface, "
        f"which is far below the ~4.2k it measured when it landed. Either it "
        f"is no longer reaching the registry (a SILENT no-op — the served "
        f"schemas are still correct, just fat), or the SDK changed shape. "
        f"`_strip_schema_titles` feature-detects `_tool_manager._tools` and "
        f"`fn_metadata.output_schema`; re-check both against the installed "
        f"`mcp`."
    )


def test_the_scrub_reaches_the_attributes_it_feature_detects() -> None:
    """Names the two private paths, so an SDK bump fails HERE with an
    explanation instead of silently at the `getattr`.

    `tests/test_resident_footprint.py::_served_schemas` asserts the first
    of these for its own reasons; this states both, as the scrub's
    contract with the SDK."""
    mcp = probe_server("title-scrub-attribute-probe")

    def _probe(content: str) -> dict[str, Any]:
        return {}

    mcp.tool(name="probe", description="d")(_probe)

    registry = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
    assert isinstance(registry, dict) and registry, (
        "FastMCP's tool registry is no longer at `_tool_manager._tools`. "
        "`builder._strip_schema_titles` feature-detects this and returns "
        "quietly, so the only symptom would be schemas growing ~4.2k chars "
        "with no diff in this repo."
    )
    (tool,) = registry.values()
    assert isinstance(tool.parameters, dict), (
        "`Tool.parameters` is no longer a plain mutable dict; the inputSchema "
        "leg of the scrub cannot mutate it in place."
    )
    assert isinstance(tool.fn_metadata.output_schema, dict), (
        "`Tool.fn_metadata.output_schema` is no longer a dict. The scrub "
        "mutates it IN PLACE because `Tool.output_schema` is a "
        "`cached_property` over it — see the test below for why that "
        "matters."
    )


async def test_assigning_a_new_output_schema_would_be_silently_ignored() -> None:
    """Why the output leg is mutated in place, measured instead of asserted.

    `Tool.output_schema` is a `cached_property` over
    `fn_metadata.output_schema`. Two facts, both checked here against the
    installed SDK rather than reasoned about:

    1. The cache is COLD when `_strip_schema_titles` runs — nothing reads
       `Tool.output_schema` between `add_tool` and the end of
       `_register_tools`. So assignment would work today.
    2. On a WARM cache assignment is silently ignored and the titles ship,
       while in-place mutation is correct either way.

    Together those say the obvious-looking version is right by accident of
    ordering. Anyone tidying `_strip_titles(output_schema)` into
    `fn_metadata.output_schema = stripped(...)` would see every test pass
    and would have removed the only thing protecting the scrub from a
    future SDK that warms the cache during registration. This is that
    reason, in executable form."""

    def _probe(content: str) -> dict[str, Any]:
        return {}

    def _fresh() -> tuple[Any, Any]:
        mcp = probe_server("cached-property-probe")
        mcp.tool(name="probe", description="d")(_probe)
        return mcp, mcp._tool_manager._tools["probe"]

    _, cold = _fresh()
    assert "output_schema" not in cold.__dict__, (
        "`Tool.output_schema` is now cached during registration. The scrub "
        "still works — it mutates in place — but the margin this test "
        "documents has just become load-bearing rather than theoretical."
    )

    mcp, tool = _fresh()
    _ = tool.output_schema  # warm the cache
    tool.fn_metadata.output_schema = {
        k: v for k, v in tool.fn_metadata.output_schema.items() if k != "title"
    }
    (served,) = await mcp.list_tools()
    assert "title" in _blob(_output_schema(served)), (
        "assigning a new `output_schema` on a warm cache now reaches the "
        "wire. The in-place mutation in `_strip_schema_titles` is still "
        "correct, but this test no longer explains why it is written that way."
    )

    mcp, tool = _fresh()
    _ = tool.output_schema  # warm the cache the same way
    tool.fn_metadata.output_schema.pop("title", None)  # IN PLACE
    (served,) = await mcp.list_tools()
    assert "title" not in _blob(_output_schema(served)), (
        "in-place mutation no longer reaches the wire through a warm "
        "`cached_property` — `_strip_schema_titles` needs re-designing."
    )


# ---------------------------------------------------------------------------
# Why it cannot change behaviour
# ---------------------------------------------------------------------------


_VOLATILE = {
    "id",
    "created",
    "updated",
    "last_verified_at",
    "use_token",
    "matched_id",
    "pending_id",
}


def _stable(obj: Any) -> Any:
    """Drop the fields that differ between two runs (ULIDs, timestamps),
    so two transcripts can be compared for equality rather than eyeballed."""
    if isinstance(obj, dict):
        return {
            k: ("<volatile>" if k in _VOLATILE else _stable(v)) for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_stable(v) for v in obj]
    return obj


async def test_the_served_schema_is_not_on_the_call_path(tmp_path: Path) -> None:
    """The structural reason the scrub is safe, proven by corruption.

    Call-time validation runs through `fn_metadata.arg_model`; the emitted
    dict is advertising copy. So two servers running the same call
    sequence must produce the same transcript even when one of them has
    had its served schema vandalised far past anything the scrub does —
    a property renamed to a wrong type, another deleted outright,
    `required` pointed at a parameter that does not exist.

    Deleting an annotation is a strict subset of that. If this test ever
    fails, the scrub is not the safe edit this module claims it is, and
    neither is anything else that edits a served schema.

    The final assertion keeps the rest from proving the wrong thing:
    argument validation is still ON, it just does not read this dict."""
    control = _server(tmp_path / "control")
    vandalised = _server(tmp_path / "vandalised")

    schema = vandalised._tool_manager._tools["memory_write"].parameters
    schema["properties"]["content"] = {"type": "integer"}
    del schema["properties"]["scopes"]
    schema["required"] = ["nonexistent"]

    calls: list[tuple[str, dict[str, Any]]] = [
        ("memory_write", {"content": "uv drives the build here", "scopes": ["tools"]}),
        # reject: transient marker
        ("memory_write", {"content": "we merged the PR today", "scopes": ["tools"]}),
        # reject: duplicate of the first write
        ("memory_write", {"content": "uv drives the build here", "scopes": ["tools"]}),
        # reject: staged pending, the user-inference path
        (
            "memory_write",
            {
                "content": "User prefers terse comments",
                "scopes": ["learning-style"],
                "category": "user-inference",
            },
        ),
        ("memory_search", {"query": "uv build"}),
        ("memory_scope_overview", {}),
    ]

    async def transcript(mcp: Any) -> list[Any]:
        return [_stable((await mcp.call_tool(name, args))[1]) for name, args in calls]

    assert await transcript(control) == await transcript(vandalised), (
        "vandalising the served inputSchema changed what calls return, so the "
        "emitted schema IS on the call path and no edit to it is safe."
    )

    # ...and the statuses actually exercised are the ones claimed above, so a
    # transcript of six identical error strings could not pass this.
    rows = await transcript(_server(tmp_path / "third"))
    statuses = [row.get("status") for row in rows[:4]]
    assert statuses == [
        "committed",
        "transient_warning",
        "duplicate",
        "pending",
    ], f"the battery stopped covering the commit and reject paths: {statuses}"
    # The two reads are shaped like reads, not like a repeated error.
    assert "result" in rows[4] and "scopes" in rows[5]

    with pytest.raises(Exception) as excinfo:
        await control.call_tool("memory_record_use", {"outcome": "applied"})
    assert "memory_ids" in str(excinfo.value), (
        "a missing required argument was accepted, so the comparison above "
        "proves nothing: validation would be off rather than reading a "
        "different source than the served schema."
    )


async def test_structured_output_survives_the_scrub(tmp_path: Path) -> None:
    """`structuredContent` is a wire-shape promise, and dropping it is the
    obvious wrong way to shrink `outputSchema`.

    Registering with `structured_output=False` would have removed it from
    every tool result. Scrubbing keeps it: `FuncMetadata.convert_result`
    tests `output_schema is not None` and validates through `output_model`,
    so a schema with its title deleted is still a schema that turns
    structured output on."""
    mcp = _server(tmp_path)
    tools = await mcp.list_tools()

    missing = [t.name for t in tools if _output_schema(t) is None]
    assert not missing, (
        f"{missing} no longer serve an outputSchema. Scrubbing titles must "
        f"not disable structured output — that is a wire-shape change, not a "
        f"size optimisation."
    )

    result = await mcp.call_tool(
        "memory_write", {"content": "the venv lives at .venv", "scopes": ["tools"]}
    )
    structured = result[1]
    assert isinstance(structured, dict) and structured, (
        "the call no longer returns structuredContent alongside the text block"
    )


async def test_client_side_validation_is_unaffected(tmp_path: Path) -> None:
    """`mcp/client/session.py` runs `jsonschema.validate(structuredContent,
    outputSchema)` on every result. `title` is annotation-only in JSON
    Schema, so this holds by construction — measured anyway, both ways,
    because "by construction" is how wire regressions get shipped.

    The rejection half is the one that matters: a scrubbed schema that
    accepted everything would also pass the acceptance half."""
    mcp = _server(tmp_path)
    tools = {t.name: t for t in await mcp.list_tools()}

    result = await mcp.call_tool(
        "memory_write", {"content": "ruff runs in pre-commit", "scopes": ["tools"]}
    )
    jsonschema_validate(result[1], _output_schema(tools["memory_write"]))

    search = _output_schema(tools["memory_search"])
    jsonschema_validate({"result": []}, search)
    with pytest.raises(ValidationError):
        jsonschema_validate({"result": "not-a-list"}, search)


# ---------------------------------------------------------------------------
# The two ways a recursive delete goes wrong
# ---------------------------------------------------------------------------


def test_a_parameter_named_title_survives_the_scrub() -> None:
    """The reason `_strip_titles` is structure-aware instead of four lines.

    Values under `properties` are keyed by CALLER-CHOSEN names. A naive
    recursive walk deletes any key spelled `title`, so a tool that ever
    grows a `title` parameter would silently stop advertising it: the
    schema keeps validating, the tool keeps working, and the parameter
    becomes undiscoverable. No tool has one today — which is precisely why
    this has to be a test and not a comment, since nothing else in the
    suite would notice the day one appears."""
    schema: dict[str, Any] = {
        "type": "object",
        "title": "probeArguments",
        "properties": {
            "title": {"type": "string", "title": "Title"},
            "body": {"type": "string", "title": "Body"},
        },
        "required": ["title"],
    }
    _strip_titles(schema)

    assert schema["properties"]["title"] == {"type": "string"}, (
        "the parameter NAMED `title` was deleted from `properties` — a naive "
        "recursive walk, and a silent wire regression"
    )
    assert "title" not in schema, "the schema's own title annotation survived"
    assert schema["properties"]["body"] == {"type": "string"}
    assert schema["required"] == ["title"], "`required` was rewritten"


def test_nested_schema_titles_are_reached() -> None:
    """The other direction: titles that hide inside `anyOf` / `items`.

    `memory_record_use.claim_excerpts` is `list[str | None] | None`, which
    pydantic emits as nested `anyOf`/`items`. A shallow scrub would leave
    those, and they are the ones a reader is least likely to spot."""
    schema: dict[str, Any] = {
        "title": "probeArguments",
        "properties": {
            "claim_excerpts": {
                "anyOf": [
                    {"items": {"title": "Item", "type": "string"}, "type": "array"},
                    {"type": "null"},
                ],
                "title": "Claim Excerpts",
            }
        },
    }
    _strip_titles(schema)
    assert _title_count(schema) == 0
    assert "title" not in json.dumps(schema), (
        "a title survived inside a nested `anyOf`/`items` branch"
    )


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------


async def test_the_scrub_does_not_leak_between_servers(tmp_path: Path) -> None:
    """Hundreds of tests build servers in one process, so a scrub that
    reached shared state would corrupt unrelated ones.

    Each registration builds a fresh `arg_model` and therefore a fresh
    schema dict, so the mutation is per-instance — asserted here in both
    directions: two bettermemory servers agree and are both scrubbed, and
    an unrelated `FastMCP` constructed AFTER a scrub still has its titles.
    The second half is what would catch a scrub that had found its way
    onto a class attribute or a cached model."""
    first = await _server(tmp_path).list_tools()
    second = await _server(tmp_path).list_tools()

    assert {t.name: _input_schema(t) for t in first} == {
        t.name: _input_schema(t) for t in second
    }
    assert not any(_title_count(_input_schema(t)) for t in second)

    unrelated = probe_server("unrelated-after-the-scrub")

    def _probe(content: str, flag: bool = False) -> dict[str, Any]:
        return {}

    unrelated.tool(name="probe", description="d")(_probe)
    (tool,) = await unrelated.list_tools()
    assert _title_count(_input_schema(tool)) > 0, (
        "an unrelated FastMCP built after a bettermemory server has no titles "
        "either — the scrub is reaching shared state, not this server's own "
        "registry."
    )


async def test_the_tool_display_title_is_untouched(tmp_path: Path) -> None:
    """`Tool.title` is the MCP display-name field, a sibling of `name` and
    `description` — not a schema annotation. The scrub walks schemas only,
    and confusing the two would erase a client-facing label."""
    mcp = _server(tmp_path)
    registry = mcp._tool_manager._tools
    assert {tool.title for tool in registry.values()} == {None}, (
        "a tool's display title changed. The scrub is only supposed to reach "
        "schema-internal annotations, and `Tool.title` is a sibling of `name`."
    )
    # The `name` and `description` siblings are equally reachable from a
    # careless walk, and equally client-facing.
    assert all(tool.name and tool.description for tool in registry.values())
    served = {t.name for t in await mcp.list_tools()}
    assert served == set(registry), "the scrub changed which tools are served"
