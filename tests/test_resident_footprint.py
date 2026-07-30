"""The aggregate resident footprint, and a ceiling on the part nothing capped.

Three resident budgets were policed in isolation and nothing summed them:
the MCP `instructions` block and the lean tool-description total (both
capped in `tests/test_server.py`), and the wire cost measured by
`bench/toolcost/run.py`. Two resident surfaces were governed by nothing at
all — the served `inputSchema`/`outputSchema` JSON and the plugin skill's
frontmatter. A parameter added to a tool grew what every client pays on
every turn, and nothing in the suite could notice.

RESIDENT means paid before the conversation starts, on every turn,
including the turns that never touch memory:

    instructions + lean tool descriptions + serialized inputSchema
    + serialized outputSchema + plugin skill frontmatter

The skill BODY is deliberately NOT summed. The available-skills listing a
client renders carries only the frontmatter; the body of
`plugin/skills/bettermemory/SKILL.md` loads on activation, and it dwarfs
its own frontmatter — `test_the_skill_body_is_excluded_and_would_breach_the_ceiling`
derives that rather than asserting it here. Of the two frontmatter
readings, this module counts the FULL `name` + `description` block (the
lines between the `---` delimiters, each including its newline
terminator), not the description value alone: the name line is served
alongside the description, so the block is what a client pays.

TWO TIERS, so that one edit answers to one budget. The `instructions`
block and the lean descriptions already carry individually-ratcheted caps
with their own recalibration law — `_DESC_BUDGET_CEILING` in
`tests/test_server.py` and the instructions ceiling above it. If this
module's ceiling also bound them, one description edit would fail two
tests under two different rules and the author would have to guess which
law applies. So:

* `_REMAINDER_CEILING` binds ONLY the previously-uncapped remainder —
  serialized inputSchema + outputSchema + skill frontmatter.
* The aggregate total is REPORTED — recorded in `_FOOTPRINT_BASELINE`,
  printed by the reporting test (`pytest -s` to see it), and named in
  every failure message here — and is never asserted against a ceiling.

SERIALIZATION CONVENTION, stated so that two different numbers can never
be quoted as one. Schemas are counted as compact, key-sorted JSON
(`json.dumps(..., sort_keys=True, separators=(",", ":"))`), summed PER
TOOL, in CHARACTERS; a tool with no `outputSchema` contributes nothing.
Descriptions and instructions are counted as raw `len(text)` — the unit
the description budget in `tests/test_server.py` uses, so the description
leg here is literally the number capped there.

How that relates to the published bench figure: `measure` in
`bench/toolcost/run.py` uses the same compact key-sorted dumps, but
serializes the whole tools array as ONE blob and reports UTF-8 BYTES. Its
`full_bytes` therefore also pays for the JSON syntax and escaping around
every description and tool name, which this module counts raw, while
carrying neither the instructions block nor the skill frontmatter. The two
figures land within a few hundred characters of each other at HEAD by
coincidence, which is exactly why the reporting test prints both, labelled,
and pins the structural reason they differ. Neither is derivable from the
other; quote them separately.
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path
from typing import Any, NamedTuple

from mcp.server.fastmcp import FastMCP

from bettermemory.builder import build_server
from bettermemory.config import (
    BehaviorConfig,
    Config,
    ProposalsConfig,
    StorageConfig,
)
from bettermemory.session import SessionState
from bettermemory.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "plugin" / "skills" / "bettermemory" / "SKILL.md"


class Footprint(NamedTuple):
    """One measurement of the resident surface, leg by leg.

    `tool_count` is carried for diagnosis only — it explains a schema jump
    ("a tool was added") in a failure message. The authority on how many
    tools register is `tests/test_tool_surface.py`; nothing here pins it.
    """

    instructions: int
    descriptions: int
    input_schemas: int
    output_schemas: int
    skill_frontmatter: int
    tool_count: int

    @property
    def uncapped_remainder(self) -> int:
        """The legs this module governs: schemas + skill frontmatter."""
        return self.input_schemas + self.output_schemas + self.skill_frontmatter

    @property
    def total(self) -> int:
        """Reported, never capped here — see the module docstring."""
        return self.instructions + self.descriptions + self.uncapped_remainder


# Measured live at HEAD against the shipping-default (lean) surface.
# DIAGNOSTIC, in the spirit of `_DESC_BASELINE` in `tests/test_server.py`:
# nothing asserts these numbers, so a stale row degrades a failure message
# and never the verdict. Re-measure it in the same commit as anything that
# moves a leg — the breakdown below is the only map a reader gets from "the
# remainder is over" to "the thing you just typed".
_FOOTPRINT_BASELINE = Footprint(
    instructions=1_608,
    descriptions=26_334,
    input_schemas=7_077,
    output_schemas=1_770,
    skill_frontmatter=759,
    tool_count=18,
)

# --- the ceiling, and the arithmetic behind it ------------------------------
#
# The remainder measured 9,606 chars at HEAD (7,077 + 1,770 + 759).
#
# The headroom is measured, not guessed. `_scheduled_param_costs` registers
# the three tool parameters this upgrade plan schedules against a real
# FastMCP server and reads their cost straight out of the emitted schema:
#
#     memory_write    acknowledge_user_claim  bool, default False    93
#     episode_search  include_bodies          bool, default True     76
#     episode_search  ids                     list[str] | None      106
#                                                            total  275
#
# Rounded up to a 300-char reserve: 9,606 + 300 = 9,906, and the ceiling is
# the next round thousand. That leaves 394 chars of headroom — the reserve
# plus roughly one more parameter of the widest shape measured above, which
# is the margin for a parameter whose name runs longer than planned (cost
# grows with the name: pydantic repeats it in the generated `title`).
#
# Anything past that is a deliberate recalibration, same ceremony as the
# description ceiling: re-measure `_FOOTPRINT_BASELINE` in the same commit
# and move this literal to a new round number. That includes the ratchet
# DOWN — stripping the pydantic `title` keys from the served schemas is
# expected to cut ~2k chars off this remainder.
_REMAINDER_CEILING = 10_000
# Reserve for the two scheduled additions above, live-checked by
# `test_the_scheduled_parameters_still_fit_the_reserve`.
_SCHEDULED_PARAM_RESERVE = 300
# Soft line: crossing it warns instead of failing, so the pressure is
# visible to whoever caused it. Set one `ids`-shaped parameter (the widest
# measured) below the ceiling — crossing it means the next parameter does
# not fit, which is the moment to react rather than the moment to discover.
_REMAINDER_PRESSURE = _REMAINDER_CEILING - 110


def _blob(obj: Any) -> str:
    """The serialization convention, in one place. Compact and key-sorted,
    so a server that pretty-prints or reorders its JSON cannot score
    differently from one that does not."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _skill_parts() -> tuple[str, str]:
    """`(frontmatter, body)` of the plugin skill.

    The frontmatter is the lines between the `---` delimiters, each
    including its newline terminator — the delimiters themselves are
    markup, not payload. Hand-parsed rather than pulled through a YAML
    dependency, the same way `tests/test_plugin.py` reads this file."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with a `---` delimiter"
    end = text.find("\n---\n", 4)
    assert end > 0, "SKILL.md frontmatter is unterminated"
    return text[4 : end + 1], text[end + len("\n---\n") :]


async def _measure(mcp: Any) -> Footprint:
    """Measure an already-built server. Split out from `_lean_server` so the
    negative tests can re-measure the SAME code path after growing a
    schema, rather than a re-implementation of it."""
    tools = await mcp.list_tools()
    frontmatter, _ = _skill_parts()
    return Footprint(
        instructions=len(mcp.instructions or ""),
        descriptions=sum(len(t.description or "") for t in tools),
        input_schemas=sum(len(_blob(t.inputSchema)) for t in tools),
        output_schemas=sum(
            len(_blob(t.outputSchema)) for t in tools if t.outputSchema is not None
        ),
        skill_frontmatter=len(frontmatter),
        tool_count=len(tools),
    )


def _lean_server(tmp_path: Path, *, full_surface: bool = False) -> Any:
    """The shipping default is the lean surface, so that is what the resident
    footprint means. Constructed the same way as the description budget's
    server in `tests/test_server.py` — explicit `full_tool_surface=False`
    (an explicitly-built `Config` defaults to the full surface) and a
    default `ProposalsConfig`, so `memory_proposals` does not auto-surface
    and change the denominator."""
    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(full_tool_surface=full_surface),
        proposals=ProposalsConfig(),
    )
    return build_server(config=cfg, store=Store(tmp_path), state=SessionState())


def _breakdown(now: Footprint) -> str:
    """Which leg moved against `_FOOTPRINT_BASELINE`, and by how much."""
    rows = []
    for leg in Footprint._fields:
        was = getattr(_FOOTPRINT_BASELINE, leg)
        cur = getattr(now, leg)
        delta = "" if cur == was else f"   ({cur - was:+d})"
        rows.append(f"  {leg}: {was} -> {cur}{delta}")
    rows.append(
        f"  uncapped remainder: {_FOOTPRINT_BASELINE.uncapped_remainder} -> "
        f"{now.uncapped_remainder}  (ceiling {_REMAINDER_CEILING})"
    )
    rows.append(
        f"  AGGREGATE TOTAL: {_FOOTPRINT_BASELINE.total} -> {now.total}  "
        f"(reported, not capped here)"
    )
    return "\n".join(rows)


def _bench_style_chars(tools: list[Any]) -> int:
    """The other convention, for the labelled side-by-side: the whole tools
    array as one blob, the way `bench/toolcost/run.py` measures it (that
    runner reports UTF-8 bytes; chars keep this comparable to the rest of
    this module)."""
    wire = [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema,
            **({"outputSchema": t.outputSchema} if t.outputSchema is not None else {}),
        }
        for t in tools
    ]
    return len(_blob(wire))


# The three parameters the upgrade plan schedules, as a real signature, so
# their cost is read out of the SDK's own emitted schema instead of guessed
# from the shape of a neighbour. Names and types mirror the plan exactly —
# a longer name costs more, so a rename here is a re-measurement.
_SCHEDULED_PARAMS = ("acknowledge_user_claim", "include_bodies", "ids")


def _scheduled_param_signature(
    content: str,
    acknowledge_user_claim: bool = False,
    include_bodies: bool = True,
    ids: list[str] | None = None,
) -> dict:
    """Probe only — never registered on a real server. `content` stands in
    for a required parameter so the emitted schema has the same shape as a
    tool's (a `required` list plus a `properties` map)."""
    return {}


def _param_cost(schema: dict, name: str) -> int:
    """What one parameter costs in the serialized schema.

    Measured by deleting the property and re-serializing, rather than by
    diffing two probe tools: the schema's own `title` carries the tool
    name, so a diff across two differently-named tools charges the
    parameter for the difference in tool names."""
    without = copy.deepcopy(schema)
    del without["properties"][name]
    required = without.get("required")
    if isinstance(required, list) and name in required:
        without["required"] = [r for r in required if r != name]
    return len(_blob(schema)) - len(_blob(without))


async def _scheduled_param_costs() -> dict[str, int]:
    """Live cost of each scheduled parameter, from a throwaway server."""
    mcp = FastMCP("resident-footprint-probe")
    mcp.tool(name="probe", description="parameter-cost probe")(
        _scheduled_param_signature
    )
    (tool,) = await mcp.list_tools()
    return {name: _param_cost(tool.inputSchema, name) for name in _SCHEDULED_PARAMS}


def _served_schemas(mcp: Any) -> dict:
    """The registry FastMCP serves `inputSchema` from, so a test can grow a
    parameter the way a code change would.

    This reaches through a private attribute deliberately. It is the same
    path the planned `title`-stripping scrub has to use — the SDK exposes
    no hook — so if this assertion ever fires, that plan item needs
    re-designing and this is the cheapest place to find out."""
    manager = getattr(mcp, "_tool_manager", None)
    registry = getattr(manager, "_tools", None)
    assert isinstance(registry, dict) and registry, (
        "FastMCP's tool registry is no longer at `_tool_manager._tools`. The "
        "schema-growth guard below reaches through it to grow a parameter, and "
        "the planned title-scrub would mutate the same path — re-check both "
        "against the installed SDK before assuming either still works."
    )
    return registry


def _grow_one_parameter(mcp: Any, tool_name: str, index: int) -> None:
    """Add one boolean parameter to a registered tool's served schema.

    Exactly the wire-visible effect of adding an optional flag to a handler
    signature: a new entry in `properties`, no change to `required`. The
    synthetic name is sized close to the real ones, so each injection costs
    within a few chars of what a real flag costs (cost scales with the name,
    which pydantic repeats in the generated `title`)."""
    schema = _served_schemas(mcp)[tool_name].parameters
    schema["properties"][f"acknowledge_probe_{index:02d}"] = {
        "default": False,
        "title": f"Acknowledge Probe {index:02d}",
        "type": "boolean",
    }


# ---------------------------------------------------------------------------
# The aggregate — reported, never capped here
# ---------------------------------------------------------------------------


async def test_aggregate_resident_footprint_is_measured_and_reported(
    tmp_path: Path,
) -> None:
    """The number the project had never computed: everything a client pays
    before the first turn, in one figure, with the legs it decomposes into.

    Asserts only what a REPORT must be true of — that the arithmetic closes
    and that the measurement is not vacuous. Capping the total is
    deliberately not done here; see the module docstring on double
    governance."""
    mcp = _lean_server(tmp_path)
    fp = await _measure(mcp)

    # The arithmetic closes: no leg is measured and then dropped.
    legs = (
        fp.instructions,
        fp.descriptions,
        fp.input_schemas,
        fp.output_schemas,
        fp.skill_frontmatter,
    )
    assert fp.total == sum(legs), (
        f"the reported total ({fp.total}) is not the sum of the legs "
        f"({sum(legs)}) — a leg is measured and then dropped, which is the "
        f"failure this module exists to end.\n{_breakdown(fp)}"
    )
    assert fp.uncapped_remainder == (
        fp.input_schemas + fp.output_schemas + fp.skill_frontmatter
    ), (
        f"the governed remainder ({fp.uncapped_remainder}) is not schemas + "
        f"frontmatter; the ceiling below is no longer binding what this "
        f"module claims it binds.\n{_breakdown(fp)}"
    )

    # Non-vacuity. A measurement that silently starts reading an empty
    # surface would sail under every ceiling in this file. This is not a
    # tool-count pin — `tests/test_tool_surface.py` owns the count — it is a
    # floor under "we measured something".
    assert fp.tool_count >= 10, (
        f"only {fp.tool_count} tools were measured; the resident-footprint "
        f"guards would pass vacuously.\n{_breakdown(fp)}"
    )
    for leg in ("instructions", "descriptions", "input_schemas", "output_schemas"):
        assert getattr(fp, leg) > 0, f"the {leg} leg measured zero"
    assert fp.skill_frontmatter > 0

    bench_style = _bench_style_chars(await mcp.list_tools())
    # Structural, so the module docstring's "not interchangeable" claim is
    # derived rather than asserted: the bench blob pays JSON syntax around
    # every name and description, and carries neither the instructions block
    # nor the skill frontmatter.
    assert bench_style > fp.descriptions + fp.input_schemas + fp.output_schemas

    print(
        "\nresident footprint (chars, lean surface):\n"
        f"{_breakdown(fp)}\n"
        f"  --- other convention, NOT the same number ---\n"
        f"  whole tools array as one blob (bench/toolcost style): {bench_style}"
    )


# ---------------------------------------------------------------------------
# The guard: a ceiling on the legs nothing else caps
# ---------------------------------------------------------------------------


async def test_uncapped_remainder_stays_under_its_ceiling(tmp_path: Path) -> None:
    """The served schemas and the skill frontmatter were resident and
    ungoverned. This is their budget.

    It binds those legs ONLY. A description edit answers to the description
    ceiling in `tests/test_server.py`; an `instructions` edit answers to the
    truncation budget beside it. Nothing here re-arbitrates either.

    When this fires the cause is one of three things, and the breakdown says
    which: a parameter was added (input_schemas moved), a return annotation
    changed (output_schemas moved), or the skill's trigger description grew.
    The fourth possibility is that the installed MCP SDK changed how it
    serializes schemas, which moves the leg by hundreds of chars with no
    diff in this repo at all — worth checking before rewriting anything."""
    fp = await _measure(_lean_server(tmp_path))
    assert fp.uncapped_remainder <= _REMAINDER_CEILING, (
        f"the ungoverned resident legs total {fp.uncapped_remainder} chars, "
        f"over the {_REMAINDER_CEILING} ceiling. Every client pays this on "
        f"every turn. Raising the literal is a recalibration with its own "
        f"ceremony — re-measure `_FOOTPRINT_BASELINE` in the same commit and "
        f"pick a new round number; the arithmetic behind the current one is "
        f"in the comment above it.\nAgainst the recorded baseline:\n"
        f"{_breakdown(fp)}"
    )
    if fp.uncapped_remainder > _REMAINDER_PRESSURE:
        warnings.warn(
            f"the ungoverned resident legs are at {fp.uncapped_remainder} of "
            f"the {_REMAINDER_CEILING}-char ceiling — "
            f"{_REMAINDER_CEILING - fp.uncapped_remainder} chars left, which "
            f"is under one parameter. React now, while this is a warning.\n"
            f"{_breakdown(fp)}",
            stacklevel=1,
        )


async def test_the_scheduled_parameters_still_fit_the_reserve() -> None:
    """The ceiling above carries a measured reserve for the parameters this
    upgrade plan schedules — a ceiling that the next phase immediately
    recalibrates would be noise, not a guard.

    Re-measures those parameters against the real SDK every run, because
    the reserve is only meaningful if it still covers them: a renamed flag
    costs more (the name is repeated in the generated `title`), and an SDK
    that emits richer schemas raises the price of all three at once."""
    costs = await _scheduled_param_costs()
    assert set(costs) == set(_SCHEDULED_PARAMS)
    for name, cost in costs.items():
        assert cost > 0, f"{name} measured as free, so the probe is not working"
    total = sum(costs.values())
    assert total <= _SCHEDULED_PARAM_RESERVE, (
        f"the scheduled parameters now cost {total} chars of serialized "
        f"schema ({costs}), more than the {_SCHEDULED_PARAM_RESERVE}-char "
        f"reserve folded into `_REMAINDER_CEILING`. Resize the reserve and "
        f"the ceiling together, before landing them."
    )
    # The reserve must also still be spendable: ceiling minus the measured
    # remainder has to cover it, or the "no immediate recalibration" promise
    # is already broken at HEAD.
    remainder = _FOOTPRINT_BASELINE.uncapped_remainder
    assert _REMAINDER_CEILING - remainder >= total, (
        f"the recorded remainder ({remainder}) leaves "
        f"{_REMAINDER_CEILING - remainder} chars under the ceiling, less than "
        f"the {total} chars the scheduled parameters measure. The ceiling was "
        f"set with headroom that no longer exists."
    )


# ---------------------------------------------------------------------------
# Proof the guard can fail — a guard that cannot fail is not a guard
# ---------------------------------------------------------------------------


async def test_an_unbudgeted_parameter_trips_the_remainder_ceiling(
    tmp_path: Path,
) -> None:
    """Adds parameters to a real registered tool, one at a time, and shows
    the ceiling firing.

    This is the regression the guard exists for: a flag added to a handler
    signature is invisible in review (no number in the diff moves) and is
    paid by every client on every turn. The growth goes through the served
    registry and the measurement goes through `_measure`, the same function
    the guard above calls — so what fails here is the guard, not a
    re-implementation of it.

    Two things are pinned. The headroom absorbs at least the parameters the
    plan schedules, so landing them is not a recalibration. And it is
    finite: unbudgeted growth runs out of room and fails until someone
    re-records the baseline and moves the ceiling deliberately."""
    mcp = _lean_server(tmp_path)
    start = await _measure(mcp)
    assert start.uncapped_remainder <= _REMAINDER_CEILING, (
        "HEAD is already over the ceiling; this test cannot demonstrate "
        "anything until that is fixed."
    )

    absorbed = 0
    grown = start
    for index in range(24):
        _grow_one_parameter(mcp, "memory_write", index)
        grown = await _measure(mcp)
        # The mutation reached the wire: the measurement reads served
        # schemas, not a recorded table.
        assert grown.input_schemas > start.input_schemas
        assert grown.tool_count == start.tool_count
        if grown.uncapped_remainder > _REMAINDER_CEILING:
            break
        absorbed = index + 1
    else:  # pragma: no cover - only reachable if the ceiling stops binding
        raise AssertionError(
            f"24 added parameters did not breach the {_REMAINDER_CEILING} "
            f"ceiling. The guard has stopped guarding.\n{_breakdown(grown)}"
        )

    scheduled = len(_SCHEDULED_PARAMS)
    assert absorbed >= scheduled, (
        f"the headroom absorbs only {absorbed} added parameters, fewer than "
        f"the {scheduled} this plan schedules — the ceiling would fail on "
        f"planned work. Re-derive it from the arithmetic above the literal."
    )


async def test_a_bloated_skill_frontmatter_trips_the_same_ceiling(
    tmp_path: Path,
) -> None:
    """The frontmatter leg is inside the budget, not decoration beside it.

    The skill's `description` is the one resident piece of the plugin and it
    has no maximum anywhere in the suite (`tests/test_plugin.py` pins a
    minimum, which is the opposite failure). Doubling it four times over is
    a plausible edit — trigger prose is where authors reach when a skill
    is not firing — and it lands in the same per-turn budget as a schema."""
    fp = await _measure(_lean_server(tmp_path))
    bloated = fp._replace(skill_frontmatter=fp.skill_frontmatter * 5)
    assert bloated.uncapped_remainder > _REMAINDER_CEILING, (
        f"quintupling the skill frontmatter ({fp.skill_frontmatter} -> "
        f"{bloated.skill_frontmatter}) left the remainder at "
        f"{bloated.uncapped_remainder}, still under the "
        f"{_REMAINDER_CEILING} ceiling — the frontmatter leg is not inside "
        f"the budget it is documented to be inside."
    )
    # The other legs did not move, so the breach is attributable.
    assert bloated.input_schemas == fp.input_schemas
    assert bloated.output_schemas == fp.output_schemas


async def test_the_skill_body_is_excluded_and_would_breach_the_ceiling(
    tmp_path: Path,
) -> None:
    """Why the body is out of the sum, derived rather than asserted.

    Only the frontmatter is resident; the body loads when the skill
    activates. Summing it would not be a rounding error — it would breach
    this ceiling on its own, which is the measure of how badly a
    "resident footprint" that included it would misstate the per-turn
    cost. Also pins the frontmatter reading: the block carries the `name`
    line as well as the description, so it is strictly larger than the
    description value that reading it the other way would have counted."""
    frontmatter, body = _skill_parts()
    fp = await _measure(_lean_server(tmp_path))

    assert fp.skill_frontmatter == len(frontmatter)
    assert fp.uncapped_remainder + len(body) > _REMAINDER_CEILING, (
        "the skill body no longer breaches the ceiling on its own; the "
        "resident-vs-activation distinction this module documents is worth "
        "re-checking against how much either half now costs."
    )

    description = next(
        line for line in frontmatter.splitlines() if line.startswith("description:")
    )
    assert len(description.split(":", 1)[1].strip()) < fp.skill_frontmatter


async def test_the_measurement_reads_the_served_surface(tmp_path: Path) -> None:
    """The legs respond to what actually registers.

    A footprint measured from a hard-coded table would pass every guard in
    this file forever. Building the full surface instead of the lean one
    adds nine tools' worth of schema, and the measurement has to see it —
    the same property that makes the ceiling able to notice a parameter."""
    lean = await _measure(_lean_server(tmp_path))
    full = await _measure(_lean_server(tmp_path, full_surface=True))
    for leg in ("tool_count", "input_schemas", "output_schemas", "descriptions"):
        assert getattr(full, leg) > getattr(lean, leg), (
            f"the {leg} leg reads the same ({getattr(lean, leg)}) on the lean "
            f"and full surfaces. It is not measuring what registers, so no "
            f"ceiling in this file can notice a schema change."
        )
    # The frontmatter and instructions legs are surface-independent.
    assert full.skill_frontmatter == lean.skill_frontmatter
    assert full.instructions == lean.instructions
