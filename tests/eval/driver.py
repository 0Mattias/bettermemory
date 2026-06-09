"""Live-agent driver — turn an agent's cite-decisions into the full eval trio.

`BetterMemoryAdapter` (adapters.py) deliberately reports `memory_helped_rate`
and `endorsement_rate` as `n/a` offline: fabricating `use` events from the
gold labels would just relabel recall (the circular implementation the
package refuses to ship — see __init__.py honesty constraint #2). The two
live-agent rates need a REAL agent deciding which retrieved memory it cited.

This module supplies the machinery that was the open piece before publication
(docs/eval.md, ROADMAP "publish the comparative numbers") WITHOUT touching
that honest offline `n/a`:

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
- `LiveAgent` — the real-model path that produces the publishable numbers.
  Gated behind the Anthropic SDK + an API key; raises `SystemUnavailable` when
  absent so CI takes the scripted path and the live run stays opt-in.

The honesty boundary is explicit: the trio is "computable" the moment a driver
feeds real-shaped citation events; whether those citations are a *measurement*
depends entirely on whether the agent is a real model (LiveAgent) or a script.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from bettermemory.audit import probe_for_miss, search_miss_fields, turn_audited_fields
from bettermemory.eval import EvalReport, compute_eval
from bettermemory.models import MemoryHit
from bettermemory.search import search

from .adapters import SystemUnavailable
from .workload import BASE_NOW, Workload, WorkloadProbe

_SESSION_ID = "sess_live_agent_driver"


@dataclass(frozen=True)
class Citation:
    """A memory the agent deliberately cited in its reply, plus the
    load-bearing phrase. `excerpt` MUST be a real, non-empty substring of the
    cited memory's body — that is what the Stop hook's attribution would have
    captured (hook.py), and `compute_eval` only counts a non-empty excerpt
    toward `memory_helped_rate`."""

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

    Receives the REAL ranker hits so any citation references an actually
    retrieved memory (honest by construction — you can't cite what you never
    saw)."""

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
    denominator) when the agent searched, a `use`/`applied` event per citation
    (the helped/endorsement numerators), and the `turn_audited` / `search_miss`
    audit events (the silent-miss lane). All flow through `compute_eval`, so a
    driver that supplies citations gets a non-`None` trio."""
    memories = workload.memories()
    body_by_id = {m.id: m.body for m in memories}
    ts = now.isoformat()
    events: list[dict[str, Any]] = []

    for probe in workload.probes:
        hits = search(memories, probe.query, max_results=k, now=now)
        turn = agent.decide(probe, hits)

        # Honesty guard — the Citation contract, enforced for EVERY agent (not
        # just the live one): an excerpt must be a real substring of the cited
        # memory's BODY, never merely of the truncated snippet the live agent
        # was shown. `snippet_for` truncates bodies >200 chars and appends a
        # synthetic "..."; a model echoing that ellipsis would otherwise inflate
        # the helped-rate numerator with a phrase the memory never contained
        # (and a genuine phrase past the snippet boundary would be wrongly
        # dropped). Validate against the body here, where the driver holds it.
        citations = tuple(
            c
            for c in turn.citations
            if c.excerpt and c.excerpt in body_by_id.get(c.memory_id, "")
        )

        # The agent searched iff it SAID so, OR a citation survived the body
        # guard above (a genuine citation proves it consulted memory). Applying
        # the citation-implies-searched coherence HERE — on validated citations,
        # not the raw ones — means a body-invalid excerpt that gets dropped can
        # never flip an explicit `searched=false`, so the silent-miss lane and
        # the helped-rate numerator stay consistent.
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
                    "triggered_from": "live_agent_driver",
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
    a published number (that needs `LiveAgent`)."""

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


def _parse_live_decision(text: str, hit_ids: set[str]) -> AgentTurn:
    """Parse a live model's JSON reply into an `AgentTurn`.

    Pure (no I/O) so the honesty-critical logic is unit-testable without an API
    key — only the surrounding model call stays `# pragma: no cover`. Contract:

    - `searched` is the MODEL's own explicit decision (the ``"searched"``
      boolean), NEVER derived from whether the ranker returned hits. Deriving it
      from ``bool(hits)`` would make the silent-miss lane a tautology of
      retrieval (any probe with a hit ⇒ "searched" ⇒ never a miss), turning a
      published "measurement" into a restatement of the ranker output. When the
      model omits a usable boolean, default to False — `run_driver` upgrades it
      to True iff a citation SURVIVES body validation (see below), so a citation
      can only imply "searched" once it's proven genuine, not before.
    - A citation must reference a memory the agent actually saw
      (``id in hit_ids``) — you can't cite what you never retrieved. The
      excerpt-is-a-real-body-substring invariant is enforced centrally in
      `run_driver` (which holds the bodies), so this stays snippet-agnostic.
    - Best-effort: any malformed shape (non-JSON, non-object, non-list
      citations, non-dict entries, a non-string ``excerpt``) degrades to a
      dropped field, never an exception — the live measurement must not crash
      mid-run on one odd model reply.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return AgentTurn(searched=False)
    if not isinstance(parsed, dict):
        return AgentTurn(searched=False)

    citations: list[Citation] = []
    raw = parsed.get("citations")
    if isinstance(raw, list):
        for c in raw:
            if not isinstance(c, dict):
                continue
            mid = c.get("id")
            # `excerpt` may be any JSON type — coerce safely. A non-string
            # (number/bool/array/object) is treated as absent, not `.strip()`ed
            # (which would raise AttributeError and abort the whole run).
            raw_excerpt = c.get("excerpt")
            excerpt = raw_excerpt.strip() if isinstance(raw_excerpt, str) else ""
            if isinstance(mid, str) and mid in hit_ids and excerpt:
                citations.append(Citation(memory_id=mid, excerpt=excerpt))

    # Faithfully report the model's stated decision; default False when it omits
    # the field. The "a genuine citation implies searched" coherence is applied
    # in run_driver AFTER body validation, so a body-invalid citation can't flip
    # an explicit abstention while being dropped from the count.
    searched_raw = parsed.get("searched")
    searched = searched_raw if isinstance(searched_raw, bool) else False
    return AgentTurn(searched=searched, citations=tuple(citations))


class LiveAgent:
    """The real-model path — the publishable measurement.

    A real agent is given the probe and the retrieved hits and decides, on its
    own, whether the retrieval shaped its reply and which memory it cited. That
    decision (not a gold label) is what makes `memory_helped_rate` /
    `endorsement_rate` a measurement rather than a relabeling of recall.

    Gated: requires the `anthropic` SDK and `ANTHROPIC_API_KEY`. Without both
    it raises `SystemUnavailable` (the same contract competitor adapters use),
    so CI runs the `ScriptedAgent` path and the live measurement stays an
    explicit, key-bearing opt-in. The model call itself is the one surface this
    harness cannot exercise in CI — by design, it's the live boundary.
    """

    def __init__(self, *, model: str = "claude-sonnet-4-6") -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemUnavailable(
                "LiveAgent needs ANTHROPIC_API_KEY (and the `anthropic` SDK) — "
                "set them to produce the publishable trio; CI uses ScriptedAgent."
            )
        try:
            import anthropic  # noqa: F401  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            raise SystemUnavailable(
                "LiveAgent needs the `anthropic` SDK (pip install anthropic)."
            ) from exc
        self._model = model

    def decide(
        self, probe: WorkloadProbe, hits: list[MemoryHit]
    ) -> AgentTurn:  # pragma: no cover - live boundary, not run in CI
        import anthropic  # pyright: ignore[reportMissingImports]

        client = anthropic.Anthropic()
        catalog = "\n".join(f"- id={h.id}: {h.snippet}" for h in hits)
        prompt = (
            "You are a coding agent answering a user message. Your memory store "
            f"surfaced these memories as possibly relevant:\n{catalog or '(none)'}"
            f"\n\nUser message: {probe.query}\n\nDecide for yourself: (1) did you "
            "actually need to consult stored memory to answer this — is any "
            "surfaced memory genuinely relevant, or is the query self-contained? "
            "and (2) which memories did your reply actually rely on? Reply ONLY "
            'with JSON: {"searched": <true if you consulted memory, false if the '
            'query needed none>, "citations": [{"id": <id of a memory you relied '
            'on>, "excerpt": <the load-bearing phrase, a verbatim substring of '
            'that memory>}]}. Use "searched": false with an empty citations list '
            "when no surfaced memory was relevant."
        )
        msg = client.messages.create(
            model=self._model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )
        # `searched` comes from the model's reply, NOT bool(hits) — see
        # _parse_live_decision. Deriving it from the ranker would make the live
        # silent_miss_rate a tautology of retrieval.
        return _parse_live_decision(text, {h.id for h in hits})
