"""Agent driver — turn an agent's cite-decisions into the full eval trio.

`BetterMemoryAdapter` (adapters.py) deliberately reports `memory_helped_rate`
and `endorsement_rate` as `n/a` offline: fabricating `use` events from the
gold labels would just relabel recall (the circular implementation the
package refuses to ship — see __init__.py honesty constraint #2). The two
agent-decision rates need a REAL agent deciding which retrieved memory it
cited.

This module supplies the compute-path machinery WITHOUT touching that honest
offline `n/a`:

- `Agent` protocol — given a probe and the REAL search hits, decide whether
  the agent retrieved and which hit ids it deliberately cited (with the
  load-bearing phrase).
- `run_driver(workload, agent)` — runs the real ranker for retrieval, asks the
  agent to decide, emits genuinely-shaped `search` + `use` + audit events, and
  feeds them through the published `compute_eval`, so all three rates compute.
- `ScriptedAgent` — a deterministic, hand-authored agent (a recorded
  transcript). Its citations are AUTHORED, not measured: it proves the compute
  path end-to-end and gives CI a reproducible trio, but its numbers are a
  demonstration, NOT a published measurement.

The honesty boundary is explicit: the trio is "computable" the moment a driver
feeds real-shaped citation events; whether those citations are a *measurement*
depends on whether a real agent in a real session produced them. A key-gated
`LiveAgent` (a one-shot Anthropic-API role-play of "an agent answering with
these memories") shipped in 3.7.0 and was deliberately REMOVED: it could not
run in the project's actual workflow (agent sessions there hold no raw
`ANTHROPIC_API_KEY`), it had already cost two honesty-defect fixes (3.7.1),
and a staged single-turn completion is not an agent session. The honest source
for live helped/endorsement numbers is production telemetry — `bettermemory
eval` over a real store's event log. The `Agent` protocol stays open for a
future driver wired to REAL transcripts; do not re-add a role-played one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bettermemory.audit import probe_for_miss, search_miss_fields, turn_audited_fields
from bettermemory.eval import EvalReport, compute_eval
from bettermemory.models import MemoryHit
from bettermemory.search import search

from .workload import BASE_NOW, Workload, WorkloadProbe

_SESSION_ID = "sess_agent_driver"


@dataclass(frozen=True)
class Citation:
    """A memory the agent deliberately cited in its reply, plus the
    load-bearing phrase. `excerpt` MUST be a real, non-whitespace substring of
    the cited memory's body — that is what the Stop hook's attribution would
    have captured (hook.py), and `compute_eval` only counts an excerpt that
    survives `.strip()` toward `memory_helped_rate`."""

    memory_id: str
    excerpt: str


@dataclass(frozen=True)
class AgentTurn:
    """One agent turn: did it retrieve, and what did it cite."""

    searched: bool
    citations: tuple[Citation, ...] = ()


@runtime_checkable
class Agent(Protocol):
    """Decides, for one probe, whether the agent searched and what it cited.

    Receives the REAL ranker hits so citations can reference actually
    retrieved memories — but the citation contract is NOT assumed of
    implementations: `run_driver` enforces it centrally, dropping any
    citation whose id is absent from this probe's hits ("you can't cite what
    you never saw"), whose excerpt is whitespace-only, or whose excerpt is
    not a substring of the cited body, then keeping at most ONE surviving
    citation per memory per turn (compute_eval's within-event id dedup,
    replayed here because the driver emits singleton use events), so a
    protocol-violating agent cannot inflate the trio."""

    def decide(self, probe: WorkloadProbe, hits: list[MemoryHit]) -> AgentTurn: ...


def run_driver(
    workload: Workload,
    agent: Agent,
    *,
    k: int = 5,
    now: datetime = BASE_NOW,
    session_id: str = _SESSION_ID,
) -> EvalReport:
    """Drive `agent` over `workload` and return the full `EvalReport`.

    For each probe: run the real `search`, ask the agent to decide, then emit
    genuinely-shaped events — a `search` event (the retrieval-occurrence
    denominator) when the agent searched, a `use`/`applied` event per validated
    cited MEMORY (the helped/endorsement numerators; repeat citations of one
    memory collapse to one event), and the `turn_audited` / `search_miss`
    audit events (the silent-miss lane). All flow through `compute_eval`, so a
    driver that supplies citations gets a non-`None` trio."""
    memories = workload.memories()
    body_by_id = {m.id: m.body for m in memories}
    ts = now.isoformat()
    events: list[dict[str, Any]] = []

    for probe in workload.probes:
        hits = search(memories, probe.query, max_results=k, now=now)
        turn = agent.decide(probe, hits)

        # Honesty guard — the Citation contract, enforced for EVERY agent
        # (run_driver is generic over Agent implementations, so the protocol's
        # invariant lives HERE, not on trust). Three validations:
        #   1. the cited id must be among THIS probe's ranker hits — you
        #      can't cite what you never saw. A hit-absent citation would
        #      otherwise mint a helped-rate numerator with no retrieval
        #      denominator behind it (1/0) and, via the coherence upgrade
        #      below, flip an abstention into `searched`, suppressing a
        #      real silent miss.
        #   2. the excerpt must be non-WHITESPACE, matching the
        #      `excerpt.strip()` gate on compute_eval's helped numerator
        #      (eval.py). A bare " " passes truthiness AND the substring
        #      check (whitespace appears in every body), so it would flip an
        #      abstention and mint endorsement_rate 1/1 while contributing
        #      nothing to helped — a cross-layer contradiction.
        #   3. the excerpt must be a real substring of the cited memory's
        #      BODY, never merely of the truncated snippet the agent was
        #      shown. Snippets of bodies >200 chars carry synthetic "..."
        #      markers — a trailing one from `snippet_for`, and a LEADING
        #      one too when a search hit windows on the matched terms
        #      rather than the body head; a model echoing either would
        #      otherwise inflate the helped-rate numerator with a phrase the
        #      memory never contained (and a genuine phrase outside the
        #      snippet window would be wrongly dropped). Validate against
        #      the body here, where the driver holds it.
        # Then dedup by memory_id, FIRST surviving citation per memory —
        # mirroring compute_eval's within-event `seen_ids` semantics. The
        # real record_use path carries a whole turn's ids in ONE event, which
        # compute_eval dedups (its comment names memory_ids=["A", "A"] as the
        # inflation vector); the driver emits one singleton event per
        # citation, so without this dedup N repeat citations of one memory
        # would score N helped/endorsement counts where identical production
        # telemetry scores 1.
        hit_ids = {h.id for h in hits}
        cited_by_id: dict[str, Citation] = {}
        for c in turn.citations:
            if (
                c.memory_id in hit_ids
                and c.excerpt.strip()
                and c.excerpt in body_by_id.get(c.memory_id, "")
            ):
                cited_by_id.setdefault(c.memory_id, c)
        citations = tuple(cited_by_id.values())

        # The agent searched iff it SAID so, OR a citation survived the guards
        # above (a genuine citation proves it consulted memory). Applying the
        # citation-implies-searched coherence HERE — on the validated, deduped
        # survivors, not the raw citations — means a citation dropped by any
        # guard can never flip an explicit `searched=false`, so the
        # silent-miss lane and the helped-rate numerator stay consistent.
        searched = turn.searched or bool(citations)

        if searched:
            events.append(
                {
                    "kind": "search",
                    "ts": ts,
                    "session": session_id,
                    "returned": [h.id for h in hits],
                }
            )
        for cite in citations:
            events.append(
                {
                    "kind": "use",
                    "ts": ts,
                    "session": session_id,
                    "ids": [cite.memory_id],
                    "outcome": "applied",
                    "auto": False,
                    "attribution": "model",
                    "claim_excerpts": [cite.excerpt],
                    "triggered_from": "agent_driver",
                }
            )

        # Audit lane: the silent-miss verdict keys off whether a retrieval
        # happened THIS turn (the agent's decision, coherence-checked against a
        # surviving citation above), exactly as the offline adapter keys off
        # `probe.agent_searched`.
        recent: list[dict[str, Any]] = (
            [{"kind": "search", "session": session_id, "ts": ts}] if searched else []
        )
        report = probe_for_miss(
            memories, probe.query, recent_events=recent, session_id=session_id, now=now
        )
        events.append(
            {
                "kind": "turn_audited",
                "ts": ts,
                **turn_audited_fields(
                    report,
                    session_id=session_id,
                    probe_mode="hybrid",
                    assistant_present=True,
                    triggered_from="mcp_tool",
                ),
            }
        )
        if report.is_miss:
            events.append(
                {
                    "kind": "search_miss",
                    "ts": ts,
                    **search_miss_fields(
                        report,
                        session_id=session_id,
                        triggered_from="mcp_tool",
                    ),
                }
            )

    return compute_eval(memories, events, now=now)


@dataclass
class ScriptedAgent:
    """A deterministic agent built from a hand-authored per-probe script.

    The script maps a probe query → `AgentTurn`. Unlisted probes default to
    "did not search, cited nothing". This is a RECORDED TRANSCRIPT, not a
    measurement: its citations are authored, so the trio it produces proves
    the compute path is wired end-to-end and is reproducible in CI — it is not
    a published number (that needs a real agent's decisions; see the module
    docstring for why the role-played `LiveAgent` was removed)."""

    script: dict[str, AgentTurn] = field(default_factory=dict)

    def decide(self, probe: WorkloadProbe, hits: list[MemoryHit]) -> AgentTurn:
        return self.script.get(probe.query, AgentTurn(searched=False))


def default_scripted_agent(workload: Workload) -> ScriptedAgent:
    """A realistic recorded transcript over `default_workload`.

    Deliberately NOT 1:1 with retrieval (so the trio can't be mistaken for
    relabeled recall): the agent searches on both gold+searched probes but
    cites on only ONE of them — on the other it retrieved the memory yet the
    reply didn't lean on it. That makes memory_helped_rate strictly below
    recall (a retrieved-but-uncited memory is in the denominator, not the
    numerator). Excerpts are real substrings of the cited memory's body (the
    honest claim-excerpt shape)."""
    by_key = {f.key: m for f, m in zip(workload.facts, workload.memories())}

    def cite(key: str, excerpt: str) -> Citation:
        mem = by_key[key]
        # Guard the honesty invariant at construction: the excerpt must really
        # appear in the cited memory's body.
        assert excerpt in mem.body, f"excerpt {excerpt!r} not in {key!r} body"
        return Citation(memory_id=mem.id, excerpt=excerpt)

    return ScriptedAgent(
        script={
            # Searched the formatter rule and the reply leaned on it — cited.
            "ruff format black formatting codebase": AgentTurn(
                searched=True, citations=(cite("formatter", "ruff format"),)
            ),
            # Searched the platform fact and retrieved it, but the reply didn't
            # actually depend on it — retrieval WITHOUT a citation, so
            # helped_rate stays below recall (it's not recall relabeled).
            "macos sequoia linux continuous integration runners": AgentTurn(
                searched=True
            ),
        }
    )
