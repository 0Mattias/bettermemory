"""What earns residency in a lean tool description.

The description budget in `tests/test_server.py` has a ceiling, a pressure
warning and a de-duplication invariant, and its own docstring names the gap
all three share: "the ceiling only ever measures size, never whether the
surface still teaches what it must." Rule 3 there asks a ratchet-down to
prove the prose that left is still taught somewhere a caller reaches — and
until 7.16.0 every ratchet answered that in a commit message. This module is
the half that was missing.

THE RESIDENCY RULE, stated once so the three guards below read against it:

    A resident tool description pays for what decides a CALL, never for
    what the RESPONSE already hands back.

A `Returns {a, b, c, d}` key list is billed on every turn, forever, to tell a
model something the tool result hands it for free at the exact moment it is
needed. Field prose survives here only where it changes a decision — what to
pass, whether to trust a truncated read, how to read a zero. Everything else
belongs in `docs/api.md`, which is read by the human planning a call and
costs nothing per turn.

The rule generalises the 2026-08-04 reclamation, which collapsed an 888-char
span and kept "only the glosses that decide WHICH edge type to use, the one
thing a caller cannot infer from the schema".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from ._mcp import call_tool as _mcp_call
from .test_server import _lean_descriptions

pytestmark = pytest.mark.anyio

_API_MD = Path(__file__).resolve().parents[1] / "docs" / "api.md"

# A brace group holding four or more bare identifiers and essentially no
# prose. `{20,}` keeps the regex off short inline shapes before the identifier
# test even runs.
_BRACE_GROUP = re.compile(r"\{([^{}]{20,})\}")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# The size bar, and why it is not zero. A four-name inline shape is
# point-of-call reference — the same thing the 888-char collapse deliberately
# KEPT — and a guard that fires on 32 characters stops being a budget and
# becomes a tripwire on unrelated work, which is the failure mode rule 1 of
# the ceiling law was written against. At 100+ the enumeration is no longer a
# cue: it is the response envelope restated in full, and the response is
# right there.
_ENUMERATION_BAR = 100


def _bare_enumerations(desc: str) -> list[str]:
    """Brace groups in `desc` that restate a response envelope."""
    found: list[str] = []
    for match in _BRACE_GROUP.finditer(desc):
        parts = [p.strip().strip("`'\" ") for p in match.group(1).split(",")]
        idents = [p for p in parts if _IDENT.match(p)]
        # "essentially no prose": at most one member may be something other
        # than a bare identifier, so `{scope: count}`-style annotated shapes
        # and sentences containing a brace do not trip this.
        if len(parts) >= 4 and len(idents) >= max(4, len(parts) - 1):
            if len(match.group(0)) >= _ENUMERATION_BAR:
                found.append(match.group(0))
    return found


async def test_no_lean_description_enumerates_a_return_shape(
    tmp_path: Path,
) -> None:
    """G1 — the class guard. No lean description restates its own envelope.

    This is a RATCHET, not a style note: 7.16.0 removed 1,661 characters of
    exactly this shape, and without a guard the next feature adds its return
    keys back one tool at a time, which is how the surface reached 15
    characters of slack twice in six days."""
    descs = await _lean_descriptions(tmp_path)
    offenders = {
        name: found
        for name, desc in descs.items()
        if (found := _bare_enumerations(desc))
    }
    assert not offenders, (
        "lean description(s) restate a response envelope the caller is "
        f"handed anyway (>= {_ENUMERATION_BAR} chars of bare keys):\n"
        + "\n".join(
            f"  {name}: {span}" for name, spans in offenders.items() for span in spans
        )
        + "\n\nThese are paid on EVERY turn. Put the field list in "
        "docs/api.md, keep only the prose that decides a call, and if the "
        "doc does not already carry every key, add it there first — "
        "test_the_destination_actually_carries_it checks that it does."
    )


def _doc_return_enumeration(doc: str, tool: str) -> str:
    """The braced return shape `docs/api.md` publishes for `tool`.

    Anchored on the tool's own `### \u0060tool(` heading and the first
    `Returns` line inside that section, so a brace group belonging to a
    neighbouring tool can never stand in for a missing one."""
    start = doc.find(f"### `{tool}(")
    assert start != -1, f"docs/api.md has no section heading for {tool}"
    end = doc.find("\n### ", start + 1)
    section = doc[start : end if end != -1 else len(doc)]
    returns = section.find("Returns")
    assert returns != -1, f"docs/api.md's {tool} section states no return shape"
    # Deliberately NOT `_BRACE_GROUP`: that one refuses nested braces so it
    # cannot be fooled by prose, and the doc's own shapes legitimately nest
    # (`scopes: {scope: count}`). Here the backticks are the delimiter.
    brace = re.search(r"`(\{.*?\})`", section[returns:], re.DOTALL)
    assert brace is not None, (
        f"docs/api.md's {tool} section has a Returns line with no `{{...}}` "
        "enumeration — the description no longer carries one either"
    )
    return brace.group(1)


async def test_the_destination_actually_carries_it(tmp_path: Path) -> None:
    """G2 — the cut spans are still taught where a caller reaches.

    Rule 3 of the ceiling law earns a ratchet-down only if "the prose that
    left is still taught somewhere a caller reaches, and the commit names
    where each cut span went." This is that clause, mechanised.

    It reads the LIVE response and asserts the tool's `Returns {...}`
    enumeration in `docs/api.md` is COMPLETE — never a recorded constant,
    because a constant drifts in the same direction as the doc and the two
    would then agree about something false.

    Why the enumeration and not merely a mention anywhere in the file: once
    the description stops listing the envelope, the doc's list is the only
    list, and an incomplete list is the failure that is actually available
    here. It is not hypothetical. When this guard was written, api.md's
    `memory_scope_overview` enumeration was missing `curation_unmeasured` —
    the prose below it discussed the field at length, so a "named somewhere
    in the doc" check would have passed while the one surface a reader scans
    for the response shape stayed wrong. The repair landed in the same
    commit, ahead of the cut that cites it.
    """
    from bettermemory.builder import build_server
    from bettermemory.config import Config, StorageConfig
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    server: Any = build_server(
        config=Config(storage=StorageConfig(directory=str(tmp_path))),
        store=Store(tmp_path),
        state=SessionState(),
    )
    doc = _API_MD.read_text()

    overview = await _mcp_call(server, "memory_scope_overview", {})
    written = await _mcp_call(
        server,
        "episode_write",
        {"body": "destination check", "takeaway": "one line"},
    )
    rows = await _mcp_call(server, "episode_search", {})
    rows = rows.get("result", rows) if isinstance(rows, dict) else rows

    live: dict[str, set[str]] = {
        "memory_scope_overview": set(overview),
        "episode_write": set(written),
        "episode_search": set(rows[0]) if rows else set(),
    }
    assert live["episode_search"], (
        "episode_search returned no row, so this guard checked nothing for "
        "it — the fixture must actually produce one."
    )

    missing = {}
    for tool, keys in live.items():
        enumeration = _doc_return_enumeration(doc, tool)
        absent = sorted(
            k for k in keys if not re.search(rf"\b{re.escape(k)}\b", enumeration)
        )
        if absent:
            missing[tool] = absent
    assert not missing, (
        "the `Returns {...}` enumeration in docs/api.md is missing keys the "
        "tool actually returns, and the description no longer enumerates "
        "them — so the response shape is taught correctly NOWHERE a caller "
        "reaches:\n"
        + "\n".join(f"  {tool}: {keys}" for tool, keys in missing.items())
        + "\n\nAdd them to that line in docs/api.md. A reclamation that "
        "cites a stale destination is a subtraction wearing a budget's "
        "clothes."
    )


# The decision cues that survived the 7.16.0 cut, keyed on CONTENT rather
# than on a line number or a length, so a reword that keeps the teaching
# passes and a quiet deletion does not. One entry per span whose removal
# would change what a caller does — not per span that merely reads well.
_MUST_STILL_TEACH: dict[str, list[tuple[str, str]]] = {
    "memory_scope_overview": [
        ("if `total` is 0, skip", "the session-start short-circuit"),
        ("never read a listed leg's 0 as clean", "how to read an unmeasured 0"),
        ("actionable audit backlog", "which counts are worth acting on"),
        ("where did X go?", "the deliberate-trim signal"),
        ("`null` on the very first session", "the delta view's empty case"),
    ],
    "episode_write": [
        ("episode_search(swarm_id=", "what passing swarm_id buys the caller"),
        ("corrupts the file", "why the caps are not advisory"),
        ("TRANSIENT_PHRASE_MARKERS", "why this tier exists at all"),
    ],
    "episode_search": [
        ("MOST-RECENT N", "which end of an over-cap window survives"),
        ("never worktree-filtered", "the explicit-selector isolation contract"),
        ("False OMITS", "how to scan cheaply before re-reading"),
    ],
    "episode_promote": [
        ("source episode is deleted", "the destructive half of a promote"),
        ("keeps the episode so you can retry", "how to recover from a cancel"),
        ("session close is when", "the state-channel minting moment"),
    ],
}


async def test_the_surface_still_teaches_what_it_must(tmp_path: Path) -> None:
    """G3 — the counter-valve to the ratchet.

    A ceiling measures size and nothing else, so it rubber-stamps a cut that
    removed teaching along with bytes — "a subtraction wearing a budget's
    clothes", in rule 3's words. Pinning the surviving decision cues by
    content is what makes the ratchet-down falsifiable: cut prose and the
    budget test goes greener, cut TEACHING and this one goes red."""
    descs = await _lean_descriptions(tmp_path)
    lost: list[str] = []
    for tool, cues in _MUST_STILL_TEACH.items():
        desc = descs.get(tool)
        assert desc is not None, f"{tool} left the lean surface"
        for phrase, why in cues:
            if phrase not in desc:
                lost.append(f"  {tool}: {why} — lost the cue {phrase!r}")
    assert not lost, (
        "a description got smaller by teaching less, which is the one way a "
        "ratchet-down is not earned:\n" + "\n".join(lost)
    )
