"""System adapters for the comparative harness.

An adapter exposes two things: a `capabilities()` matrix (always available,
even for systems we can't execute here) and a `run()` that produces a
`RunResult` or raises `SystemUnavailable`.

Only `BetterMemoryAdapter` runs in this repo — it drives the real `search`
and `probe_for_miss` code over a workload and feeds the genuinely-derived
audit events through `compute_eval`. The competitor adapters are honest
stubs: they raise `SystemUnavailable` (the package isn't a dependency here)
and carry a capability row sourced from each project's public
documentation. The comparative point doesn't need them to execute — it's
structural: only bettermemory logs all three signals the published trio
requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from bettermemory.audit import probe_for_miss, search_miss_fields, turn_audited_fields
from bettermemory.eval import EvalReport, compute_eval
from bettermemory.search import search

from .workload import BASE_NOW, Workload

# Session id used for the simulated audit pass. Single session — the
# workload models one continuous turn-by-turn transcript.
_SESSION_ID = "sess_comparative_eval"


class SystemUnavailable(Exception):
    """Raised by an adapter that can't execute in this environment.

    Carries a human-readable `reason` (missing package, absent API key)
    so the comparative report can explain *why* a row has no measured
    numbers without pretending the system failed a benchmark.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Capabilities:
    """Whether a system logs the three signals the published trio needs.

    The trio (``memory_helped_rate`` / ``endorsement_rate`` /
    ``silent_miss_rate``) is computable only when all three are present:

    - ``logs_retrieval``: per-hit retrieval occurrences (the denominator
      for helped-rate, and the signal an audit cross-references).
    - ``logs_endorsement``: load-bearing tagging — *which* retrieved
      memory the agent deliberately cited, distinct from auto-applied
      context. Without it endorsement-rate has no numerator.
    - ``has_audit_hook``: a post-turn probe that can flag a *silent miss*
      (relevant memory existed, agent never retrieved it).
    """

    logs_retrieval: bool
    logs_endorsement: bool
    has_audit_hook: bool
    notes: str = ""

    @property
    def can_compute_trio(self) -> bool:
        return self.logs_retrieval and self.logs_endorsement and self.has_audit_hook

    def to_dict(self) -> dict[str, Any]:
        return {
            "logs_retrieval": self.logs_retrieval,
            "logs_endorsement": self.logs_endorsement,
            "has_audit_hook": self.has_audit_hook,
            "can_compute_trio": self.can_compute_trio,
            "notes": self.notes,
        }


@dataclass
class RunResult:
    """Outcome for one system on one workload.

    `ran` is False for systems that couldn't execute here; their metric
    fields stay None and `unavailable_reason` explains why. `capabilities`
    is always populated so the capability matrix is complete regardless.
    """

    name: str
    capabilities: Capabilities
    ran: bool
    k: int
    unavailable_reason: str | None = None
    probes_total: int = 0
    gold_total: int = 0
    recalled: int = 0
    recall_at_k: float | None = None
    eval_report: EvalReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ran": self.ran,
            "k": self.k,
            "unavailable_reason": self.unavailable_reason,
            "capabilities": self.capabilities.to_dict(),
            "probes_total": self.probes_total,
            "gold_total": self.gold_total,
            "recalled": self.recalled,
            "recall_at_k": self.recall_at_k,
            "eval": self.eval_report.to_dict() if self.eval_report else None,
        }


@runtime_checkable
class SystemAdapter(Protocol):
    """A memory system the harness can describe and (maybe) run."""

    name: str

    def capabilities(self) -> Capabilities: ...

    def run(self, workload: Workload, *, k: int) -> RunResult: ...


class BetterMemoryAdapter:
    """Runs the real bettermemory retrieval + audit code over a workload.

    Retrieval lane: `search` over the corpus, recall@k against gold ids.

    Audit lane: for every probe, `probe_for_miss` decides a silent-miss
    verdict from (high-relevance hit exists) x (agent retrieved this turn).
    Each probe emits a `turn_audited` event; a miss verdict also emits a
    `search_miss` event. Those genuinely-derived events go through
    `compute_eval`, which yields a real `silent_miss_rate` (with Wilson
    CI). `memory_helped_rate` and `endorsement_rate` come back as ``None``
    — no `use` events exist offline, which is the honest answer, not 0.0.
    """

    name = "bettermemory"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            logs_retrieval=True,
            logs_endorsement=True,
            has_audit_hook=True,
            notes=(
                "record_use logs auto-vs-load-bearing with claim excerpts; "
                "probe_for_miss / turn_audited is the post-turn audit hook."
            ),
        )

    def run(self, workload: Workload, *, k: int) -> RunResult:
        memories = workload.memories()

        # --- retrieval lane: recall@k over gold probes ---
        gold = workload.gold_probes
        recalled = 0
        for probe in gold:
            gid = workload.gold_id(probe)
            hits = search(memories, probe.query, max_results=k, now=BASE_NOW)
            if gid is not None and gid in {h.id for h in hits}:
                recalled += 1
        recall = (recalled / len(gold)) if gold else None

        # --- audit lane: real probe_for_miss -> compute_eval events ---
        events: list[dict[str, Any]] = []
        ts = BASE_NOW.isoformat()
        for probe in workload.probes:
            recent: list[dict[str, Any]] = (
                [{"kind": "search", "session": _SESSION_ID, "ts": ts}]
                if probe.agent_searched
                else []
            )
            report = probe_for_miss(
                memories,
                probe.query,
                recent_events=recent,
                session_id=_SESSION_ID,
                now=BASE_NOW,
            )
            events.append(
                {
                    "kind": "turn_audited",
                    "ts": ts,
                    **turn_audited_fields(
                        report,
                        session_id=_SESSION_ID,
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
                            session_id=_SESSION_ID,
                            triggered_from="mcp_tool",
                        ),
                    }
                )

        eval_report = compute_eval(memories, events, now=BASE_NOW)

        return RunResult(
            name=self.name,
            capabilities=self.capabilities(),
            ran=True,
            k=k,
            probes_total=len(workload.probes),
            gold_total=len(gold),
            recalled=recalled,
            recall_at_k=recall,
            eval_report=eval_report,
        )


@dataclass
class _UnavailableAdapter:
    """Base for competitor stubs: a capability row + a reason it can't run.

    The matrix row is the deliverable; `run()` raises so the harness never
    fabricates numbers for a system we didn't actually execute.
    """

    name: str
    _caps: Capabilities
    _reason: str = field(default="")

    def capabilities(self) -> Capabilities:
        return self._caps

    def run(self, workload: Workload, *, k: int) -> RunResult:
        raise SystemUnavailable(self._reason)


def mem0_adapter() -> _UnavailableAdapter:
    """mem0 (mem0ai) — vector + graph store with LLM fact extraction."""
    return _UnavailableAdapter(
        name="mem0",
        _caps=Capabilities(
            logs_retrieval=True,
            logs_endorsement=False,
            has_audit_hook=False,
            notes=(
                "search() returns ranked memories (retrieval is logged), but "
                "there is no load-bearing/cited tagging and no post-turn "
                "silent-miss audit — so the endorsement and silent-miss lanes "
                "have no source signal."
            ),
        ),
        _reason="package 'mem0ai' is not installed in this environment (pip install mem0ai); capability row from public docs",
    )


def server_memory_adapter() -> _UnavailableAdapter:
    """The reference MCP knowledge-graph memory server (@modelcontextprotocol/server-memory)."""
    return _UnavailableAdapter(
        name="server-memory",
        _caps=Capabilities(
            logs_retrieval=True,
            logs_endorsement=False,
            has_audit_hook=False,
            notes=(
                "read_graph / search_nodes surface entities (retrieval is "
                "observable), but nothing records which node shaped a reply "
                "and there is no miss-audit primitive."
            ),
        ),
        _reason="reference MCP memory server is a separate Node package, not runnable from this Python harness; capability row from public docs",
    )


def claude_mem_adapter() -> _UnavailableAdapter:
    """claude-mem — session-summary / compaction memory."""
    return _UnavailableAdapter(
        name="claude-mem",
        _caps=Capabilities(
            logs_retrieval=False,
            logs_endorsement=False,
            has_audit_hook=False,
            notes=(
                "injects compacted session context wholesale rather than "
                "emitting per-hit retrieval occurrences, so even the "
                "retrieval denominator the trio needs is absent."
            ),
        ),
        _reason="claude-mem is a separate Node/CLI tool, not runnable from this Python harness; capability row from public docs",
    )


def default_adapters() -> list[SystemAdapter]:
    """bettermemory first (the one that runs), then the competitor stubs."""
    return [
        BetterMemoryAdapter(),
        mem0_adapter(),
        server_memory_adapter(),
        claude_mem_adapter(),
    ]
