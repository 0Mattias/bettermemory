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
# no verdict reads these numbers, so a stale row degrades a failure message
# and never the outcome. Re-measure it in the same commit as anything that
# moves a leg — the breakdown below is the only map a reader gets from "the
# remainder is over" to "the thing you just typed".
#
# That property is load-bearing and was briefly lost: the scheduled-reserve
# test read `uncapped_remainder` from this literal instead of measuring, so
# the one guard whose job is to announce that the promised headroom is gone
# could not see it go. It reported the reserve intact while a landed phase
# had already spent 93 of it. Keep verdicts on live measurements; this table
# is for the reader.
#
# Re-measured 2026-07-31 for Phase 7's takeaway-only episode reads.
# `input_schemas` 7,170 -> 7,352 is the reserve below being spent to the
# character: `include_bodies` (76) + `ids` (106) on `episode_search`.
# `descriptions` 26,860 -> 27,295 is +247 of that edit (the two new
# parameter bullets plus `ids` joining the worktree carve-out's
# enumeration) on top of +188 that had gone unrecorded since the trust
# recut moved `memory_search` — the drift this table exists to make
# attributable, caught by re-measuring rather than by re-typing.
#
# The same commit's state-channel convention (Phase 7 / G2) then moved
# `descriptions` a second time, 27,295 -> 27,398, all of it the +103 on
# `episode_promote`. It is recorded here rather than left for the next
# reader precisely because two DESC edits landing together is the shape
# that produced the unrecorded +188 above: whoever measures second sees a
# number that already includes the other lane's edit and has no way to
# split it after the fact. Note which leg did NOT move — `input_schemas`
# is untouched by G2, because a convention is prose and prose is free of
# the remainder this file governs. Only G1 spent the reserve.
#
# RE-MEASURED for the footprint phase's schema title scrub (E5), which is
# the edit the comment below the table had been waiting for since this
# module was written. `builder._strip_schema_titles` deletes pydantic's
# auto-generated `title` annotations from the served schemas after
# registration:
#
#     input_schemas   7,352 -> 5,233   (-2,119, 88 title keys)
#     output_schemas  1,770 -> 1,077   (-693, 21 title keys)
#     remainder       9,881 -> 7,069   (-2,812)
#
# Both governed legs moved and NOTHING else did — `descriptions`,
# `instructions` and `skill_frontmatter` are untouched, because the scrub
# reaches schemas only. That is the shape of a saving that costs nothing:
# no prose was cut, no parameter removed, no tool degated. `title` is a
# display annotation in JSON Schema; nothing validates against it and no
# client behaviour reads it, so the bytes were pure rent. See
# `tests/test_schema_title_scrub.py` for the proofs, including the one
# that matters most here — the scrub's failure mode is a SILENT no-op, and
# the ratcheted ceiling below is the second net under it (the un-scrubbed
# 9,881 does not fit under 7,500).
#
# RE-MEASURED AGAIN for the footprint phase's description cuts (E1), the
# other half of the same phase:
#
#     descriptions   27,398 -> 25,535  (-1,863)
#
# and nothing else moved, which is the mirror of the scrub above: E5 touched
# only schemas, E1 touches only prose, so the two legs are independently
# attributable. The cut spans are duplicated policy that each gate's reject
# `hint` already teaches verbatim, plus `memory_audit_turn`'s caller
# reference — a Stop-hook-dispatched tool whose DESC the model pays for every
# turn and is told never to act on. `tests/test_server.py`'s
# `_DESC_BASELINE` carries the per-tool attribution and the argument for
# each; its ceiling ratcheted 27,500 -> 26,000 in the same commit.
#
# RE-MEASURED once more after the phase's closing follow-ups: 25,535 ->
# 25,773 (+238), from restoring `status="stale"` to the update/verify
# descriptions and re-pointing memory_write's `duplicate` bullet at its
# hint. Recorded because it is the exact rot this table invites: the
# assertion below is DIAGNOSTIC, so a stale row never fails, and the
# +238 sat unnoticed in four PUBLISHED surfaces (README, internals, the
# toolcost artifact and its README) until a recon lane recomputed them.
# The rule this file states at the top is not optional — re-measure in
# the same commit as anything that moves a leg, including the small
# follow-up edits that feel too minor to bother.
# RE-MEASURED again, 25,773 -> 25,749 (-24), when `DESC_MEMORY_AUDIT_TURN`
# lost the clause " through the MCP channel". The clause was false: the
# shipped Stop hook dispatches `uvx bettermemory audit-turn --quiet`, the
# CLI, and `plugin/hooks/hooks.json` is where that is written down. So the
# saving is a correction rather than a trim, which is the only kind of
# description edit that is free. Recorded here in the same commit as the
# edit, per the rule above — a -24 is exactly the size of drift this
# diagnostic table has already been caught carrying.
# RE-MEASURED again, descriptions 25,749 -> 25,890 (+141) and input_schemas
# 5,233 -> 5,293 (+60), when `memory_update` grew the `acknowledge_user_claim`
# parameter and the paragraph documenting it. The commit that made that edit
# re-measured `_DESC_BASELINE` in tests/test_server.py — memory_update 1,892
# -> 2,033, the same +141 — and left THIS table alone, so the two sibling
# tables disagreed about one description for a whole release window. That is
# the rot the rule at the top of this file exists to prevent, caught by an
# audit rather than by the suite, because the assertion below is DIAGNOSTIC
# and a stale row never fails. Re-measuring one of two tables is the same
# defect as re-measuring neither: when a leg moves, grep for every table that
# records it.
# RE-MEASURED 2026-08-04 for the truncation write-gate, and this time BOTH
# tables moved in the same commit — descriptions 25,890 -> 25,419 (-471) and
# input_schemas 5,293 -> 5,353 (+60). The -471 is a -658 and a +187 netted:
# `DESC_MEMORY_LINKS_TAIL` collapsed to a four-name type index (its mechanics
# were already verbatim in docs/api.md), funding the gate's clause and its
# `acknowledge_truncation` escape. The +60 is that escape on the served schema
# — the same 60 a post-scrub boolean flag costs in the table below, which is
# the fourth independent confirmation that the price list there is still
# accurate rather than inherited.
_FOOTPRINT_BASELINE = Footprint(
    instructions=1_608,
    descriptions=25_419,
    input_schemas=5_353,
    output_schemas=1_077,
    skill_frontmatter=759,
    tool_count=18,
)

# --- the ceiling, and the arithmetic behind it ------------------------------
#
# The remainder measured 9,606 chars when this ceiling was set (7,077 +
# 1,770 + 759). It is 9,881 now, and the whole difference is the reserve
# below being spent on exactly what it was reserved for:
#
#     memory_write    acknowledge_user_claim  bool, default False    93
#     episode_search  include_bodies          bool, default True     76
#     episode_search  ids                     list[str] | None      106
#                                                            total  275
#
# The write-path phase landed the first; Phase 7's takeaway-only episode
# reads landed the other two. The costs are not historical trivia — they
# are re-measured off the served schemas every run by
# `test_the_landed_parameters_cost_what_the_reserve_promised`, so "the
# reserve was spent as budgeted" stays a checked claim rather than a note.
#
# Rounded up to a 300-char reserve: 9,606 + 300 = 9,906, and the ceiling is
# the next round thousand. That left 394 chars of headroom — the reserve
# plus roughly one more parameter of the widest shape measured above, which
# is the margin for a parameter whose name runs longer than planned (cost
# grows with the name: pydantic repeated it in the generated `title`).
#
# RATCHETED DOWN, 10,000 -> 7,500, by the schema title scrub (E5). This is
# the recalibration the paragraph below always described, taken in the
# direction it explicitly covers, and it is the reason the literal moves
# rather than the headroom simply widening: a ceiling left at 10,000 over a
# 7,069 remainder is not a budget, it is 2,931 chars of licence that nobody
# argued for. The saving was bought once, from the SDK's annotations, and
# banking it is the whole point of taking it.
#
# THE ARITHMETIC, re-derived rather than inherited:
#
#     remainder now                              7,069
#     ceiling (next round 500 above it)          7,500
#     headroom                                     431
#
# The parameters below are re-measured post-scrub and they got CHEAPER,
# because the generated `title` was most of what a short flag cost:
#
#     memory_write    acknowledge_user_claim  bool         93 -> 60
#     episode_search  include_bodies          bool         76 -> 51
#     episode_search  ids                     list|None   106 -> 92
#                                                 total   275 -> 203
#
# So 431 chars is room for roughly seven boolean flags (58 each on the
# served surface now) or four of the widest `ids` shape — several times the
# single parameter the pre-scrub ceiling had left, which is what "restores
# headroom" meant. The negative test below re-derives the count rather than
# trusting this comment.
#
# Anything past that is a deliberate recalibration, same ceremony as the
# description ceiling: re-measure `_FOOTPRINT_BASELINE` in the same commit
# and move this literal to a new round number. That includes the ratchet
# DOWN — this move is that clause being exercised for the first time, and
# it is worth saying plainly that the ceremony is identical in both
# directions. The measured total moving is the POINT of a down-ratchet,
# where in an up-ratchet it would be the thing under suspicion.
#
# One more thing this ceiling now backstops. The scrub's failure mode is a
# SILENT no-op: it feature-detects a private SDK attribute, and if a future
# `mcp` moves it the function logs at debug and returns, leaving correct
# schemas that are 2,812 chars fatter with no diff anywhere in this repo.
# 7,069 + 2,812 = 9,881, which fits under 10,000 and does NOT fit under
# 7,500. Ratcheting is therefore not only bookkeeping — it is what makes
# this file able to notice.
_REMAINDER_CEILING = 7_500
# Reserve for parameters this plan schedules but has NOT landed. Zero:
# all three are on the wire (see `_LANDED_PARAMS`), so there is nothing
# left to hold room for. A future phase that schedules a parameter
# re-populates `_SCHEDULED_PARAMS` and resizes this together with the
# ceiling — pricing it against the probe BEFORE writing the signature is
# the point of keeping the machinery.
_SCHEDULED_PARAM_RESERVE = 0
# What the reserve was spent on, and where each one has to be visible for
# the spend to be real: a parameter that never reached the `_handlers.py`
# facade costs nothing here precisely because it is not on the wire, so
# this table doubles as a facade check.
_LANDED_PARAMS: tuple[tuple[str, str], ...] = (
    ("memory_write", "acknowledge_user_claim"),
    ("episode_search", "include_bodies"),
    ("episode_search", "ids"),
    # Landed 2026-08-04 with the truncation write-gate. Measured 60 on the
    # served schema — identical to `acknowledge_user_claim`, which is what a
    # post-scrub boolean flag costs when the name is the same length (both
    # are 22 characters). This one was NOT drawn from the reserve: the
    # reserve is 0 and has been since the last phase closed, so the 60 came
    # out of the ceiling's standing headroom, leaving 311 under
    # `_REMAINDER_CEILING`. Named here for the facade check the table
    # doubles as — `acknowledge_truncation` has to reach
    # `ToolHandlers.memory_update` in `_handlers.py` or it costs nothing
    # here, because it would not be on the wire at all.
    ("memory_update", "acknowledge_truncation"),
)
# Re-derived post-scrub: the three measure 203 together (60 + 51 + 92),
# down from 275, because pydantic's generated `title` was between a third
# and a half of what each short flag cost. Left at 275 the assert would
# still pass — and would have quietly loosened by 26%, which is the way a
# budget stops being one. Rounded up to 210 for the same reason the
# original carried slack: a parameter's cost tracks its NAME, so a rename
# is a re-measurement, and a guard that fails on a two-character rename is
# noise rather than signal.
#
# RE-DERIVED 2026-08-04, 210 -> 270, because the SET grew rather than the
# prices: `memory_update.acknowledge_truncation` landed and measured 60, so
# the four now cost 263 (60 + 51 + 92 + 60). This is the one way this budget
# is allowed to move, and the distinction matters because the assertion's own
# failure message asserts the opposite cause ("Nobody typed anything: the SDK
# got more expensive"). Somebody did type something here. A rise with
# `_LANDED_PARAMS` unchanged still means what that message says it means.
# Rounded up by the same ~3% the original carried, for the same rename reason.
_LANDED_PARAM_BUDGET = 270
# Soft line: crossing it warns instead of failing, so the pressure is
# visible to whoever caused it. Set one `ids`-shaped parameter (the widest
# measured) below the ceiling — crossing it means the next parameter does
# not fit, which is the moment to react rather than the moment to discover.
# Re-derived with the ceiling: `ids` is 92 post-scrub, not 106, so the gap
# rounds to 100.
_REMAINDER_PRESSURE = _REMAINDER_CEILING - 100


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
        input_schemas=sum(len(_blob(_input_schema(t))) for t in tools),
        output_schemas=sum(
            len(_blob(_output_schema(t)))
            for t in tools
            if _output_schema(t) is not None
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
            "inputSchema": _input_schema(t),
            **(
                {"outputSchema": _output_schema(t)}
                if _output_schema(t) is not None
                else {}
            ),
        }
        for t in tools
    ]
    return len(_blob(wire))


# Parameters the plan schedules but has not landed, as a real signature, so
# their cost is read out of the SDK's own emitted schema instead of guessed
# from the shape of a neighbour. Names and types mirror the plan exactly —
# a longer name costs more, so a rename here is a re-measurement.
#
# EMPTY, because all three the plan scheduled are now on the wire and are
# measured against the real served surface instead (`_LANDED_PARAMS`). The
# probe stays because it is the only way to price a parameter that does not
# exist yet, which is the question a phase asks BEFORE it writes the
# signature: add the name here and to `_scheduled_param_signature`, and the
# test below prices it against whatever reserve the ceiling then carries.
_SCHEDULED_PARAMS: tuple[str, ...] = ()


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
    diffing two probe tools, which would charge the parameter for
    everything else that differs between the two schemas."""
    without = copy.deepcopy(schema)
    del without["properties"][name]
    required = without.get("required")
    if isinstance(required, list) and name in required:
        without["required"] = [r for r in required if r != name]
    return len(_blob(schema)) - len(_blob(without))


async def _scheduled_param_costs() -> dict[str, int]:
    """Live cost of each scheduled parameter, from a throwaway server.

    The probe is a bare `MCPServer`, so it never goes through
    `_register_tools` and its schema still carries pydantic's `title`
    annotations. A real server's does not — `builder._strip_schema_titles`
    deletes them at registration — and the title is between a third and a
    half of what a short flag costs. Pricing an unlanded parameter off the
    raw probe would therefore overquote it by ~50%, and the entire point of
    this machinery is to answer "what will this cost?" before the signature
    is written. So the probe is scrubbed with the same function the server
    uses, and is priced on the surface the parameter would actually land
    on."""
    mcp = probe_server("resident-footprint-probe")
    mcp.tool(name="probe", description="parameter-cost probe")(
        _scheduled_param_signature
    )
    (tool,) = await mcp.list_tools()
    schema = copy.deepcopy(_input_schema(tool))
    _strip_titles(schema)
    return {name: _param_cost(schema, name) for name in _SCHEDULED_PARAMS}


def _served_schemas(mcp: Any) -> dict:
    """The registry the SDK serves the input schema from, so a test can grow a
    parameter the way a code change would.

    This reaches through a private attribute deliberately. It is the same
    path `builder._strip_schema_titles` uses — the SDK exposes no hook —
    so if this assertion ever fires, that scrub has silently become a
    no-op and this is the cheapest place to find out. It no longer
    describes a plan: the scrub shipped, and
    `tests/test_schema_title_scrub.py` states the same contract from the
    other side."""
    manager = getattr(mcp, "_tool_manager", None)
    registry = getattr(manager, "_tools", None)
    assert isinstance(registry, dict) and registry, (
        "The SDK's tool registry is no longer at `_tool_manager._tools`. The "
        "schema-growth guard below reaches through it to grow a parameter, and "
        "the shipped title-scrub mutates the same path — re-check both "
        "against the installed SDK before assuming either still works."
    )
    return registry


def _grow_one_parameter(mcp: Any, tool_name: str, index: int) -> None:
    """Add one boolean parameter to a registered tool's served schema.

    Exactly the wire-visible effect of adding an optional flag to a handler
    signature: a new entry in `properties`, no change to `required`. The
    synthetic name is sized close to the real ones, so each injection costs
    within a few chars of what a real flag costs (cost still scales with
    the name — it is the property key).

    NO `title` KEY, and that is a re-measurement rather than a tidy-up.
    Before the scrub, pydantic emitted one and a real added flag paid for
    it, so the injection carried one too and cost 89 chars. Now
    `builder._strip_schema_titles` deletes it before any client sees it, a
    real flag costs 58, and an injection that still carried a title would
    overstate every parameter by a third — making the headroom below read
    as smaller than it is, in a test whose entire job is to report how much
    room is left."""
    schema = _served_schemas(mcp)[tool_name].parameters
    schema["properties"][f"acknowledge_probe_{index:02d}"] = {
        "default": False,
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


async def test_the_landed_parameters_cost_what_the_reserve_promised(
    tmp_path: Path,
) -> None:
    """The ceiling above was raised to hold a measured reserve for three
    parameters. All three have now landed, so the question this guard
    answers has changed from "does the reserve still cover them?" to "did
    the reserve go where it was promised?" — and the second question is
    the one that can still be answered wrongly.

    Both halves are re-measured off the REAL served surface every run,
    which makes this a facade check as much as a budget one. A parameter
    added to a handler but not to the `_handlers.py` signature is absent
    from `properties` and costs zero, so it would show up here as a
    reserve that was never spent — while the flag it was reserved for
    quietly does nothing on the wire. That defect has shipped twice.

    The reserve is also only meaningful if the SDK still prices these the
    way it did: a renamed flag costs more (the name is repeated in the
    generated `title`), and an SDK that emits richer schemas raises the
    price of all of them at once."""
    tools = {t.name: t for t in await _lean_server(tmp_path).list_tools()}

    costs: dict[str, int] = {}
    for tool_name, param in _LANDED_PARAMS:
        schema = _input_schema(tools[tool_name])
        assert param in schema["properties"], (
            f"{tool_name} does not serve `{param}`, which the ceiling above "
            f"reserved room for. Either it never reached the `_handlers.py` "
            f"facade (the schema is built from THAT signature, so a "
            f"handler-only parameter is silently dropped at call time), or it "
            f"was removed and the reserve should be retired with it. Served: "
            f"{sorted(schema['properties'])}"
        )
        costs[f"{tool_name}.{param}"] = _param_cost(schema, param)

    for name, cost in costs.items():
        assert cost > 0, f"{name} measured as free, so the measurement is broken"
    total = sum(costs.values())
    assert total <= _LANDED_PARAM_BUDGET, (
        f"the parameters the reserve was spent on now cost {total} chars of "
        f"serialized schema ({costs}), more than the {_LANDED_PARAM_BUDGET} "
        f"budgeted for them. Nobody typed anything: the SDK got more "
        f"expensive, and every other tool's schema did too. Re-measure "
        f"`_FOOTPRINT_BASELINE` and re-derive the ceiling."
    )

    # Anything the plan schedules but has NOT landed is priced against the
    # probe, and has to fit whatever reserve the ceiling still carries.
    # Currently empty — the loop is what a future phase re-populates rather
    # than re-invents.
    scheduled = await _scheduled_param_costs()
    assert set(scheduled) == set(_SCHEDULED_PARAMS)
    scheduled_total = sum(scheduled.values())
    assert scheduled_total <= _SCHEDULED_PARAM_RESERVE, (
        f"the scheduled parameters cost {scheduled_total} chars ({scheduled}), "
        f"more than the {_SCHEDULED_PARAM_RESERVE}-char reserve folded into "
        f"`_REMAINDER_CEILING`. Resize the reserve and the ceiling together, "
        f"before landing them."
    )
    # And the reserve must still be spendable: ceiling minus the remainder
    # has to cover it, or the "no immediate recalibration" promise is already
    # broken at HEAD. Measured LIVE, not read from `_FOOTPRINT_BASELINE` — the
    # recorded literal is hand-maintained, so reading it here would mean this
    # guard could only fire after someone had already noticed by hand. It
    # would report headroom that a landed phase has quietly spent.
    remainder = (await _measure(_lean_server(tmp_path))).uncapped_remainder
    assert _REMAINDER_CEILING - remainder >= scheduled_total, (
        f"the live remainder ({remainder}) leaves "
        f"{_REMAINDER_CEILING - remainder} chars under the ceiling, less than "
        f"the {scheduled_total} chars the scheduled parameters measure. The "
        f"ceiling was set with headroom that no longer exists."
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

    # At least whatever the plan still schedules, and never fewer than one:
    # `_SCHEDULED_PARAMS` is empty now that the reserve is spent, and
    # `absorbed >= 0` would be vacuously true of a ceiling already breached.
    #
    # The footprint phase's ratchet-down has now landed and this number moved
    # with it: 431 chars of headroom over a 58-char injection absorbs SEVEN,
    # against the one the pre-scrub ceiling could take. The floor stays at
    # one on purpose — it is the assertion that the ceiling has not been
    # tightened into failing on the next flag anyone adds, which is the
    # opposite failure from the one above and just as easy to ship.
    required = max(1, len(_SCHEDULED_PARAMS))
    assert absorbed >= required, (
        f"the headroom absorbs only {absorbed} added parameters, fewer than "
        f"the {required} this plan schedules — the ceiling would fail on "
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
